"""Real-CLI tests for the `schedule` MCP server (schedule-tool.md §8).

The point of the feature is that a *model* can work the schedule API, so the
only test that really settles it runs a real turn: the agent is asked for
something recurring and has to reach for `mcp__schedule__create` and get the
recurrence right, with nothing but the tool's own docstring to go on.

Auto-skipped when the relevant binary isn't installed and signed in. A real
FastAPI is served on an ephemeral port carrying the session-scoped schedule
routes, because the in-turn MCP shim is a separate process making a real HTTP
call to `http://127.0.0.1:{settings.port}` — with nothing listening it
reports "failed to reach Octopus", which reads like the model declining to
call the tool.
"""

from __future__ import annotations

import asyncio
import glob
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from apscheduler.triggers.cron import CronTrigger

from server.config import settings
from server.database import Database
from server.scheduler import ScheduleRunner
from server.session_manager import session_manager

# Widen PATH so the CLIs resolve in a non-interactive pytest run.
for _d in [
    os.path.expanduser("~/.local/bin"),
    "/usr/local/bin",
    *sorted(glob.glob(os.path.expanduser("~/.nvm/versions/node/*/bin"))),
]:
    if _d and _d not in os.environ.get("PATH", "").split(os.pathsep):
        os.environ["PATH"] = _d + os.pathsep + os.environ.get("PATH", "")


from tests.cli_gate import claude_cli_works, codex_cli_works  # noqa: E402

HAS_CLAUDE = claude_cli_works()
HAS_CODEX = codex_cli_works()


async def _serve_schedule_api() -> tuple[int, "uvicorn.Server", asyncio.Task]:
    import uvicorn
    from fastapi import FastAPI

    from server.routers import schedules as schedules_routes

    app = FastAPI()
    app.include_router(schedules_routes.session_router)

    config = uvicorn.Config(
        app, host="127.0.0.1", port=0, log_level="warning", lifespan="off"
    )
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None  # type: ignore[method-assign]
    task = asyncio.create_task(server.serve())
    for _ in range(200):
        if server.started and server.servers:
            break
        await asyncio.sleep(0.05)
    else:  # pragma: no cover - a stuck uvicorn is a failure, not a skip
        raise RuntimeError("callback API server never started")
    return server.servers[0].sockets[0].getsockname()[1], server, task


async def _bootstrap(tmp_path, monkeypatch):
    """In-memory DB, the SessionManager singleton bound to it (the routes
    resolve the singleton through a function-local import, so a private
    instance would leave the MCP shim talking to a different object graph),
    a live schedule runner, and the callback API."""
    monkeypatch.setattr(settings, "agents_dir", str(tmp_path / "agents"))
    db = Database(":memory:")
    await db.initialize()

    session_manager.sessions.clear()
    await session_manager.initialize(db)

    runner = ScheduleRunner(session_manager, db)
    await runner.initialize()

    from server.routers import schedules as schedules_routes

    monkeypatch.setattr(schedules_routes, "_db", db)
    monkeypatch.setattr(schedules_routes, "_runner", runner)

    port, server, task = await _serve_schedule_api()
    monkeypatch.setattr(settings, "port", port)

    wd = str(tmp_path / "ws")
    os.makedirs(wd, exist_ok=True)

    async def teardown() -> None:
        server.should_exit = True
        try:
            await asyncio.wait_for(task, timeout=10.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            task.cancel()
        await runner.shutdown()
        # Release the held CLI process; several of these left running is
        # enough to get the suite OOM-killed (inline-steering.md §7).
        await session_manager.stop_all_held_processes()
        await db.close()

    return db, wd, teardown


async def _turn(session_id: str, prompt: str) -> None:
    async for _event in session_manager.send_message(session_id, prompt):
        pass


@pytest.mark.skipif(not HAS_CLAUDE, reason="claude CLI not on PATH")
@pytest.mark.asyncio
async def test_real_claude_schedules_a_weekday_morning_run(tmp_path, monkeypatch):
    """"Every weekday at 9am" has to come out as a crontab expression that
    actually fires on weekdays at 9 — the docstring is the only place the
    model learns the field order."""
    db, wd, teardown = await _bootstrap(tmp_path, monkeypatch)
    try:
        agent = await db.get_system_agent()
        assert agent is not None
        session = await session_manager.create_session(
            agent_id=agent["id"], name="sched", working_dir=wd
        )

        await _turn(
            session.id,
            "Using your Octopus schedule tool, set yourself a recurring "
            "schedule: every weekday at 9am, run the task 'check the build "
            "and report failures'. Name it 'Morning build check' and use the "
            "timezone America/Los_Angeles. Don't touch the system crontab. "
            "Reply with the schedule id when it's set.",
        )

        rows = await db.load_schedules()
        assert len(rows) == 1, f"expected one schedule, got {rows}"
        row = rows[0]
        assert row["agent_id"] == agent["id"]
        assert "build" in row["prompt"].lower()
        assert row["cron"], f"expected a cron recurrence, got {row}"
        assert row["timezone"] == "America/Los_Angeles"
        # Assert the behaviour, not the spelling: "1-5" and "1,2,3,4,5" are
        # both right, and both have to fire Mon-Fri at 09:00.
        zone = ZoneInfo(row["timezone"])
        trigger = CronTrigger.from_crontab(row["cron"], timezone=zone)
        fire = trigger.get_next_fire_time(None, datetime.now(zone))
        assert fire is not None
        assert (fire.hour, fire.minute) == (9, 0), fire
        assert fire.weekday() < 5, fire
        # Set up from a conversation, so the run lands back in it.
        assert row["origin_session_id"] == session.id
    finally:
        await teardown()


@pytest.mark.skipif(not HAS_CLAUDE, reason="claude CLI not on PATH")
@pytest.mark.asyncio
async def test_real_claude_finds_and_pauses_an_existing_schedule(tmp_path, monkeypatch):
    """"Stop the queue poll for now" means list, match by name, pause — and
    pause rather than delete, which is what the tool docs push toward."""
    db, wd, teardown = await _bootstrap(tmp_path, monkeypatch)
    try:
        agent = await db.get_system_agent()
        assert agent is not None
        session = await session_manager.create_session(
            agent_id=agent["id"], name="sched", working_dir=wd
        )

        from server.routers import schedules as schedules_routes

        seeded = await schedules_routes.create_schedule_for_agent(
            agent["id"],
            "Queue poll",
            "poll the job queue and report anything stuck",
            interval_seconds=900,
            recurrence_label="Every 15m",
            origin_session_id=session.id,
        )

        await _turn(
            session.id,
            "Stop my 'Queue poll' schedule for now — I want it back later, so "
            "don't delete it. Use your Octopus schedule tools.",
        )

        rows = await db.load_schedules()
        assert [r["id"] for r in rows] == [seeded["id"]], "the schedule was deleted"
        assert rows[0]["enabled"] is False, "the schedule is still running"
    finally:
        await teardown()


@pytest.mark.skipif(not HAS_CODEX, reason="codex CLI not on PATH")
@pytest.mark.asyncio
async def test_real_codex_schedules_an_interval_run(tmp_path, monkeypatch):
    """The same tool, driven by the other harness — the MCP surface is
    harness-agnostic, and Codex learns it from its own prompt blurb."""
    db, wd, teardown = await _bootstrap(tmp_path, monkeypatch)
    try:
        from server.agent_manager import AgentManager

        am = AgentManager(db)
        agent = await am.create_agent(name="Codexy", backend="codex")
        session = await session_manager.create_session(
            agent_id=agent["id"], name="sched", working_dir=wd, backend="codex"
        )

        await _turn(
            session.id,
            "Using your Octopus schedule tool, set yourself a schedule that "
            "runs every 30 minutes with the task 'ping the status endpoint'. "
            "Don't touch the system crontab.",
        )

        rows = await db.load_schedules()
        assert len(rows) == 1, f"expected one schedule, got {rows}"
        assert rows[0]["interval_seconds"] == 1800
        assert rows[0]["agent_id"] == agent["id"]
    finally:
        await teardown()
