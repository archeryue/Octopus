"""Tests for the agent-scoped ScheduleRunner (agent-refactor.md §5.3/§5.6)."""

import pytest

from server.database import Database
from server.models import SessionStatus
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
    assert ran["prompt"].endswith("do it")
    assert ran["prompt"].startswith("[scheduled:daily — Every 1m]")
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


@pytest.mark.asyncio
async def test_fire_appends_into_live_origin_session(setup, monkeypatch):
    """A schedule created from a `/schedule` chat command (origin_session_id set)
    appends each fire into that same, still-live session — queued via
    start_message — and does NOT create or archive a throwaway session."""
    mgr, db, runner = setup
    agent = await db.get_system_agent()

    # The session the command was issued from (a normal user session).
    origin = await mgr.create_session(agent["id"], name="chat")

    started: dict = {}

    async def fake_start(session_id, prompt):
        started["session_id"] = session_id
        started["prompt"] = prompt

    created: list[str] = []
    orig_create = mgr.create_session

    async def tracking_create(agent_id, *args, **kwargs):
        sess = await orig_create(agent_id, *args, **kwargs)
        created.append(sess.id)
        return sess

    monkeypatch.setattr(mgr, "start_message", fake_start)
    monkeypatch.setattr(mgr, "create_session", tracking_create)

    await db.save_schedule(
        schedule_id="sch1",
        agent_id=agent["id"],
        name="daily",
        prompt="summarize",
        interval_seconds=60,
        created_at="2026-01-01T00:00:00+00:00",
        origin_session_id=origin.id,
    )

    await runner._fire("sch1", agent["id"], "summarize", origin.id)

    # Ran in the origin session, not a fresh one.
    assert started["session_id"] == origin.id
    # …carrying the marker every machine-injected turn in Octopus carries, so
    # the agent can tell its 07:00 run from something the user typed.
    assert started["prompt"].startswith("[scheduled:daily — Every 1m]")
    assert started["prompt"].endswith("summarize")
    # The session the fire ran in is recorded, so "last run" can open it.
    assert (await db.load_schedules())[0]["last_run_session_id"] == origin.id
    assert created == []  # no throwaway session materialized
    # Origin session is left intact (it's a user session, never archived).
    assert mgr.get_session(origin.id) is not None
    # last_run_at recorded.
    assert (await db.load_schedules())[0]["last_run_at"] is not None


@pytest.mark.asyncio
async def test_fire_falls_back_to_fresh_session_when_origin_gone(setup, monkeypatch):
    """If the recorded origin session no longer exists (deleted/archived), the
    fire degrades to materializing a fresh schedule-origin session that
    auto-archives on idle — the schedule keeps working regardless."""
    mgr, db, runner = setup
    agent = await db.get_system_agent()

    ran: dict = {}

    async def fake_send(session_id, prompt):
        ran["session_id"] = session_id
        if False:
            yield

    monkeypatch.setattr(mgr, "send_message", fake_send)

    await db.save_schedule(
        schedule_id="sch2",
        agent_id=agent["id"],
        name="daily",
        prompt="do it",
        interval_seconds=60,
        created_at="2026-01-01T00:00:00+00:00",
        origin_session_id="ghost-session-that-was-deleted",
    )

    await runner._fire("sch2", agent["id"], "do it", "ghost-session-that-was-deleted")

    # A fresh schedule-origin session ran the prompt and was auto-archived.
    fired_sid = ran["session_id"]
    assert fired_sid != "ghost-session-that-was-deleted"
    rows = await db.load_sessions(include_archived=True)
    fired = next(r for r in rows if r["id"] == fired_sid)
    assert fired["origin"] == "schedule"
    assert fired["archived"] is True
    assert mgr.get_session(fired_sid) is None


@pytest.mark.asyncio
async def test_add_job_carries_origin_session_id_to_fire(setup):
    """`_add_job` threads origin_session_id from the schedule row into the job's
    args so the fire knows which session to append into."""
    mgr, db, runner = setup
    agent = await db.get_system_agent()
    await db.save_schedule(
        schedule_id="sch3",
        agent_id=agent["id"],
        name="n",
        prompt="p",
        interval_seconds=60,
        created_at="t",
        origin_session_id="origin-abc",
    )
    row = (await db.load_schedules())[0]
    await runner.add(row)

    job = runner._scheduler.get_job("sch3")
    assert job is not None
    # args = [schedule_id, agent_id, prompt, origin_session_id]
    assert job.args[3] == "origin-abc"


@pytest.mark.asyncio
async def test_cron_schedule_registers_cron_trigger(setup):
    """A schedule with a cron expression registers an APScheduler CronTrigger
    in the agent's timezone (not an interval job)."""
    from apscheduler.triggers.cron import CronTrigger

    mgr, db, runner = setup
    agent = await db.get_system_agent()
    await db.save_schedule(
        schedule_id="cron1",
        agent_id=agent["id"],
        name="morning",
        prompt="summarize",
        created_at="2026-01-01T00:00:00+00:00",
        cron="0 9 * * *",
        timezone="America/Los_Angeles",
        recurrence_label="Every day at 9:00 AM",
    )
    row = (await db.load_schedules())[0]
    await runner.add(row)

    job = runner._scheduler.get_job("cron1")
    assert job is not None
    assert isinstance(job.trigger, CronTrigger)


@pytest.mark.asyncio
async def test_initialize_deletes_missed_run_at_schedules(setup):
    """On startup, run_at schedules whose fire time has already passed (missed
    while the server was down) are deleted from the DB rather than silently
    dropped by APScheduler."""
    mgr, db, runner = setup
    agent = await db.get_system_agent()

    # A one-time schedule with a fire time in the distant past.
    await db.save_schedule(
        schedule_id="missed1",
        agent_id=agent["id"],
        name="missed",
        prompt="should have run already",
        created_at="2026-01-01T00:00:00+00:00",
        run_at="2020-01-01T09:00:00+00:00",
        recurrence_label="Once on Jan 1 2020",
    )
    # A normal interval schedule that should survive.
    await db.save_schedule(
        schedule_id="recurring1",
        agent_id=agent["id"],
        name="recurring",
        prompt="keep going",
        created_at="2026-01-01T00:00:00+00:00",
        interval_seconds=3600,
    )
    assert len(await db.load_schedules()) == 2

    await runner.initialize()

    remaining = await db.load_schedules()
    assert [r["id"] for r in remaining] == ["recurring1"]
    assert runner._scheduler.get_job("missed1") is None
    assert runner._scheduler.get_job("recurring1") is not None


@pytest.mark.asyncio
async def test_run_at_schedule_registers_date_trigger(setup):
    """A schedule with run_at registers an APScheduler DateTrigger."""
    from apscheduler.triggers.date import DateTrigger

    mgr, db, runner = setup
    agent = await db.get_system_agent()
    run_at = "2030-01-15T09:00:00+00:00"
    await db.save_schedule(
        schedule_id="once1",
        agent_id=agent["id"],
        name="one-time",
        prompt="do it once",
        created_at="2026-01-01T00:00:00+00:00",
        run_at=run_at,
        recurrence_label="Once on Jan 15 at 9:00 AM",
    )
    row = (await db.load_schedules())[0]
    await runner.add(row)

    job = runner._scheduler.get_job("once1")
    assert job is not None
    assert isinstance(job.trigger, DateTrigger)
    # run_at is threaded into args so _fire knows to delete after firing.
    assert job.args[4] == run_at


@pytest.mark.asyncio
async def test_run_at_schedule_deleted_after_fire(setup, monkeypatch):
    """After a one-time (run_at) schedule fires, its DB row is deleted."""
    mgr, db, runner = setup
    agent = await db.get_system_agent()

    async def fake_send(session_id, prompt):
        if False:
            yield

    monkeypatch.setattr(mgr, "send_message", fake_send)

    await db.save_schedule(
        schedule_id="once2",
        agent_id=agent["id"],
        name="one-time",
        prompt="do it once",
        created_at="2026-01-01T00:00:00+00:00",
        run_at="2030-01-15T09:00:00+00:00",
        recurrence_label="Once on Jan 15 at 9:00 AM",
    )
    assert len(await db.load_schedules()) == 1

    # Simulate fire: _fire deletes the row regardless of success.
    await runner._fire("once2", agent["id"], "do it once", run_at="2030-01-15T09:00:00+00:00")

    # Row gone from DB.
    assert await db.load_schedules() == []


@pytest.mark.asyncio
async def test_run_at_schedule_deleted_after_fire_with_origin_session(setup, monkeypatch):
    """One-time schedule fires into an origin session and is deleted from DB."""
    mgr, db, runner = setup
    agent = await db.get_system_agent()
    origin = await mgr.create_session(agent["id"], name="chat")

    started: dict = {}

    async def fake_start(session_id, prompt):
        started["session_id"] = session_id

    monkeypatch.setattr(mgr, "start_message", fake_start)

    await db.save_schedule(
        schedule_id="once3",
        agent_id=agent["id"],
        name="one-time",
        prompt="remind me",
        created_at="2026-01-01T00:00:00+00:00",
        run_at="2030-01-15T09:00:00+00:00",
        recurrence_label="Once on Jan 15 at 9:00 AM",
        origin_session_id=origin.id,
    )
    assert len(await db.load_schedules()) == 1

    await runner._fire("once3", agent["id"], "remind me", origin.id, "2030-01-15T09:00:00+00:00")

    assert started["session_id"] == origin.id
    # DB row auto-deleted after the one-time fire.
    assert await db.load_schedules() == []


@pytest.mark.asyncio
async def test_repoint_schedules_origin():
    """repoint_schedules_origin moves only schedules anchored to the old session
    and returns those (post-update) rows; unrelated schedules stay put."""
    db = Database(":memory:")
    await db.initialize()
    try:
        agent = await db.get_system_agent()
        await db.save_schedule(
            schedule_id="a",
            agent_id=agent["id"],
            name="anchored",
            prompt="p",
            created_at="t",
            interval_seconds=60,
            origin_session_id="old-sess",
        )
        await db.save_schedule(
            schedule_id="b",
            agent_id=agent["id"],
            name="other",
            prompt="p",
            created_at="t",
            interval_seconds=60,
            origin_session_id="someone-else",
        )

        # No match → empty, nothing changed.
        assert await db.repoint_schedules_origin("ghost", "new-sess") == []

        affected = await db.repoint_schedules_origin("old-sess", "new-sess")
        assert [r["id"] for r in affected] == ["a"]
        assert affected[0]["origin_session_id"] == "new-sess"

        rows = {r["id"]: r for r in await db.load_schedules()}
        assert rows["a"]["origin_session_id"] == "new-sess"
        assert rows["b"]["origin_session_id"] == "someone-else"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_schedule_recurrence_columns_roundtrip():
    """cron / timezone / recurrence_label persist and load; interval is NULL
    for a cron schedule."""
    db = Database(":memory:")
    await db.initialize()
    try:
        agent = await db.get_system_agent()
        await db.save_schedule(
            schedule_id="s1",
            agent_id=agent["id"],
            name="n",
            prompt="p",
            created_at="t",
            cron="*/15 * * * *",
            timezone="UTC",
            recurrence_label="Every 15 minutes",
        )
        row = (await db.load_schedules())[0]
        assert row["cron"] == "*/15 * * * *"
        assert row["timezone"] == "UTC"
        assert row["recurrence_label"] == "Every 15 minutes"
        assert row["interval_seconds"] is None
        assert row["run_at"] is None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_run_at_column_roundtrip():
    """run_at persists and loads correctly; cron/interval stay NULL."""
    db = Database(":memory:")
    await db.initialize()
    try:
        agent = await db.get_system_agent()
        run_at = "2030-01-15T09:00:00+00:00"
        await db.save_schedule(
            schedule_id="once-db",
            agent_id=agent["id"],
            name="one-time task",
            prompt="do it once",
            created_at="t",
            run_at=run_at,
            recurrence_label="Once on Jan 15 at 9:00 AM",
        )
        row = (await db.load_schedules())[0]
        assert row["run_at"] == run_at
        assert row["interval_seconds"] is None
        assert row["cron"] is None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_migrate_schedule_recurrence_from_legacy_shape():
    """The in-place migration rebuilds a pre-recurrence schedules table
    (NOT NULL interval, no cron column), preserving existing interval rows."""
    db = Database(":memory:")
    await db.initialize()
    try:
        agent = await db.get_system_agent()
        conn = db._conn
        # Recreate the legacy schema + a legacy interval schedule.
        await conn.executescript(
            """
            DROP TABLE schedules;
            CREATE TABLE schedules (
                id TEXT PRIMARY KEY,
                agent_id TEXT REFERENCES agents(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                prompt TEXT NOT NULL,
                interval_seconds INTEGER NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                last_run_at TEXT
            );
            """
        )
        await conn.execute(
            "INSERT INTO schedules (id, agent_id, name, prompt, interval_seconds, "
            "enabled, created_at) VALUES (?, ?, ?, ?, ?, 1, ?)",
            ("leg1", agent["id"], "legacy", "do it", 120, "t"),
        )
        await conn.commit()
        assert not await db._has_column("schedules", "cron")

        await db._migrate_schedule_recurrence()
        await db._migrate_schedule_run_at()

        assert await db._has_column("schedules", "cron")
        rows = await db.load_schedules()
        assert len(rows) == 1
        assert rows[0]["id"] == "leg1"
        assert rows[0]["interval_seconds"] == 120
        assert rows[0]["cron"] is None
        assert rows[0]["recurrence_label"] is None
        assert rows[0]["run_at"] is None

        # A cron schedule can now be inserted (interval_seconds nullable).
        await db.save_schedule(
            schedule_id="cron2",
            agent_id=agent["id"],
            name="n",
            prompt="p",
            created_at="t",
            cron="0 6 * * *",
            timezone="UTC",
            recurrence_label="Every day at 6 AM",
        )
        # Idempotent: re-running the migration is a no-op.
        await db._migrate_schedule_recurrence()
        assert len(await db.load_schedules()) == 2
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_migrate_schedule_run_at_from_legacy_shape():
    """_migrate_schedule_run_at adds run_at to a pre-existing table that lacks
    it, preserving existing rows, and is idempotent on re-run."""
    db = Database(":memory:")
    await db.initialize()
    try:
        agent = await db.get_system_agent()
        conn = db._conn
        # Simulate a pre-run_at table by dropping and recreating without run_at.
        await conn.executescript(
            """
            DROP TABLE schedules;
            CREATE TABLE schedules (
                id TEXT PRIMARY KEY,
                agent_id TEXT REFERENCES agents(id) ON DELETE CASCADE,
                origin_session_id TEXT,
                name TEXT NOT NULL,
                prompt TEXT NOT NULL,
                interval_seconds INTEGER,
                cron TEXT,
                timezone TEXT,
                recurrence_label TEXT,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                last_run_at TEXT
            );
            """
        )
        await conn.execute(
            "INSERT INTO schedules (id, agent_id, name, prompt, interval_seconds, "
            "enabled, created_at) VALUES (?, ?, ?, ?, ?, 1, ?)",
            ("old1", agent["id"], "old", "do it", 120, "t"),
        )
        await conn.commit()
        assert not await db._has_column("schedules", "run_at")

        await db._migrate_schedule_run_at()

        assert await db._has_column("schedules", "run_at")
        rows = await db.load_schedules()
        assert len(rows) == 1
        assert rows[0]["run_at"] is None

        # Idempotent.
        await db._migrate_schedule_run_at()
        assert await db._has_column("schedules", "run_at")
    finally:
        await db.close()


# ---------------------------------------------------------------- overlap


@pytest.mark.asyncio
async def test_a_fire_is_skipped_while_the_previous_one_is_still_running(
    setup, monkeypatch
):
    """A schedule that runs every 5 minutes and takes 10 would otherwise stack
    runs on top of each other until the box gives out."""
    mgr, db, runner = setup
    agent = await db.get_system_agent()

    sends: list[str] = []

    async def fake_send(session_id, prompt):
        sends.append(session_id)
        if False:
            yield

    monkeypatch.setattr(mgr, "send_message", fake_send)
    await db.save_schedule(
        schedule_id="busy1",
        agent_id=agent["id"],
        name="slow",
        prompt="do it",
        interval_seconds=60,
        created_at="2026-01-01T00:00:00+00:00",
    )

    # Fire once — it records the session it ran in…
    await runner._fire("busy1", agent["id"], "do it")
    assert len(sends) == 1
    first_sid = (await db.load_schedules())[0]["last_run_session_id"]
    assert first_sid == sends[0]

    # …and pretend that run is still going when the next tick arrives.
    stuck = await mgr.create_session(agent["id"], name="stuck")
    await db.update_schedule("busy1", last_run_session_id=stuck.id)
    stuck.status = SessionStatus.running

    await runner._fire("busy1", agent["id"], "do it")
    assert len(sends) == 1, "a second run started while the first was going"

    # Once it finishes, the schedule resumes on the next tick.
    stuck.status = SessionStatus.idle
    await runner._fire("busy1", agent["id"], "do it")
    assert len(sends) == 2


@pytest.mark.asyncio
async def test_a_gone_or_idle_previous_session_never_blocks(setup, monkeypatch):
    """The guard must not wedge a schedule: a deleted or finished session is
    not a reason to skip forever."""
    mgr, db, runner = setup
    agent = await db.get_system_agent()

    sends: list[str] = []

    async def fake_send(session_id, prompt):
        sends.append(session_id)
        if False:
            yield

    monkeypatch.setattr(mgr, "send_message", fake_send)
    await db.save_schedule(
        schedule_id="gone1",
        agent_id=agent["id"],
        name="n",
        prompt="p",
        interval_seconds=60,
        created_at="2026-01-01T00:00:00+00:00",
    )
    await db.update_schedule("gone1", last_run_session_id="deleted-long-ago")

    await runner._fire("gone1", agent["id"], "p")
    assert len(sends) == 1


@pytest.mark.asyncio
async def test_a_fire_does_not_pile_up_behind_another_fire_in_a_chat(
    setup, monkeypatch
):
    """Origin-session mode queues behind the user's own turn — that's the
    design. Queuing behind a *previous fire* is a pile-up."""
    mgr, db, runner = setup
    agent = await db.get_system_agent()
    origin = await mgr.create_session(agent["id"], name="chat")

    started: list[str] = []

    async def fake_start(session_id, prompt):
        started.append(prompt)

    monkeypatch.setattr(mgr, "start_message", fake_start)
    await db.save_schedule(
        schedule_id="chat1",
        agent_id=agent["id"],
        name="daily",
        prompt="summarize",
        interval_seconds=60,
        created_at="2026-01-01T00:00:00+00:00",
        origin_session_id=origin.id,
    )

    await runner._fire("chat1", agent["id"], "summarize", origin.id)
    assert len(started) == 1

    # Something is now waiting in that session's queue.
    origin._pending_queue.append(object())
    await runner._fire("chat1", agent["id"], "summarize", origin.id)
    assert len(started) == 1, "a second fire queued behind the first"

    origin._pending_queue.clear()
    await runner._fire("chat1", agent["id"], "summarize", origin.id)
    assert len(started) == 2
