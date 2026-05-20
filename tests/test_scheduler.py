"""Tests for the agent-scoped ScheduleRunner (agent-refactor.md §5.3/§5.6)."""

import pytest

from server.database import Database
from server.scheduler import ScheduleRunner
from server.session_manager import SessionManager


@pytest.fixture
async def setup():
    db = Database(":memory:")
    await db.initialize()
    mgr = SessionManager()
    await mgr.initialize(db)
    runner = ScheduleRunner(mgr, db)
    try:
        yield mgr, db, runner
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_fire_materializes_scheduled_session_and_auto_archives(
    setup, monkeypatch
):
    mgr, db, runner = setup
    agent = await db.get_system_agent()

    # Stub the turn — no real backend; just record what ran.
    ran: dict = {}

    async def fake_send(session_id, prompt):
        ran["session_id"] = session_id
        ran["prompt"] = prompt
        if False:  # make this an async generator without yielding events
            yield

    monkeypatch.setattr(mgr, "send_message", fake_send)

    await db.save_schedule(
        schedule_id="sch1",
        agent_id=agent["id"],
        name="daily",
        prompt="do it",
        interval_seconds=60,
        created_at="2026-01-01T00:00:00+00:00",
    )

    await runner._fire("sch1", agent["id"], "do it")

    # A fresh session ran the prompt under the agent...
    assert ran["prompt"] == "do it"
    fired_sid = ran["session_id"]
    rows = await db.load_sessions(include_archived=True)
    fired = next(r for r in rows if r["id"] == fired_sid)
    assert fired["agent_id"] == agent["id"]
    assert fired["origin"] == "schedule"

    # ...and auto-archived on idle: gone from the live map, archived in DB.
    assert mgr.get_session(fired_sid) is None
    assert fired["archived"] is True

    # last_run_at recorded.
    scheds = await db.load_schedules()
    assert scheds[0]["last_run_at"] is not None


@pytest.mark.asyncio
async def test_fire_uses_agent_id_from_job_args(setup, monkeypatch):
    """The scheduled job carries agent_id (not session_id) and each fire
    creates its own session — no persistent session reuse."""
    mgr, db, runner = setup
    agent = await db.get_system_agent()

    created_sessions: list[str] = []
    orig_create = mgr.create_session

    async def tracking_create(agent_id, *args, **kwargs):
        sess = await orig_create(agent_id, *args, **kwargs)
        created_sessions.append(sess.id)
        return sess

    async def fake_send(session_id, prompt):
        if False:
            yield

    monkeypatch.setattr(mgr, "create_session", tracking_create)
    monkeypatch.setattr(mgr, "send_message", fake_send)

    await runner._fire("schX", agent["id"], "first")
    await runner._fire("schX", agent["id"], "second")

    # Two fires → two distinct fresh sessions.
    assert len(created_sessions) == 2
    assert created_sessions[0] != created_sessions[1]
