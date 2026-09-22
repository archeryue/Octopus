"""Session-scoped schedule routes — the surface an agent's schedule tool
drives (schedule-tool.md §4).

`/api/sessions/{sid}/schedules` differs from `/api/schedules` in two ways
that matter and are tested here: the agent is *derived* from the session
(never passed, so an agent can't act for another one), and the recurrence is
stated outright and validated rather than parsed out of English.
"""

import pytest
from httpx import ASGITransport, AsyncClient

from server.agent_manager import AgentManager
from server.database import Database
from server.main import app
from server.routers import agents as agents_mod
from server.routers import schedules as schedules_mod
from server.scheduler import ScheduleRunner
from server.session_manager import session_manager

TOKEN = "changeme"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
async def client():
    db = Database(":memory:")
    await db.initialize()
    session_manager.sessions.clear()
    await session_manager.initialize(db)

    agents_mod.set_manager(AgentManager(db))
    runner = ScheduleRunner(session_manager, db)
    await runner.initialize()
    schedules_mod._db = db
    schedules_mod._runner = runner

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c

    await runner.shutdown()
    await db.close()


async def _session(client) -> str:
    resp = await client.post("/api/sessions", json={"name": "s"}, headers=HEADERS)
    assert resp.status_code == 201
    return resp.json()["id"]


async def _other_agents_schedule(client) -> str:
    """A schedule owned by a different agent — the thing this session must
    not be able to see or touch."""
    agent = await client.post(
        "/api/agents", json={"name": "Someone Else"}, headers=HEADERS
    )
    assert agent.status_code == 201
    resp = await client.post(
        f"/api/agents/{agent.json()['id']}/schedules",
        json={"name": "theirs", "prompt": "not yours", "interval_seconds": 600},
        headers=HEADERS,
    )
    assert resp.status_code == 201
    return resp.json()["id"]


# --- create ---------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_create_cron_schedule_for_this_session(client):
    sid = await _session(client)
    resp = await client.post(
        f"/api/sessions/{sid}/schedules",
        json={
            "prompt": "check the build and report failures",
            "name": "Morning build check",
            "cron": "0 9 * * 1-5",
            "timezone": "America/Los_Angeles",
        },
        headers=HEADERS,
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["cron"] == "0 9 * * 1-5"
    assert data["timezone"] == "America/Los_Angeles"
    assert data["recurrence_label"] == "Weekdays at 09:00"
    assert data["interval_seconds"] is None
    assert data["enabled"] is True
    # Derived, not passed: the schedule belongs to this session's agent...
    sess = (await client.get(f"/api/sessions/{sid}", headers=HEADERS)).json()
    assert data["agent_id"] == sess["agent_id"]
    # ...and by default its fires land back in this conversation.
    assert data["origin_session_id"] == sid
    # Registered with the runner, so it has a next fire time.
    assert data["next_run_at"]


@pytest.mark.asyncio
async def test_create_interval_schedule_outside_this_session(client):
    sid = await _session(client)
    resp = await client.post(
        f"/api/sessions/{sid}/schedules",
        json={"prompt": "poll the queue", "interval_seconds": 900, "in_session": False},
        headers=HEADERS,
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["interval_seconds"] == 900
    assert data["recurrence_label"] == "Every 15m"
    # in_session=False → each fire gets a throwaway session instead.
    assert data["origin_session_id"] is None
    # The name falls back to the first line of the prompt.
    assert data["name"] == "poll the queue"


@pytest.mark.asyncio
async def test_create_one_time_schedule(client):
    from datetime import datetime, timedelta, timezone

    sid = await _session(client)
    when = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    resp = await client.post(
        f"/api/sessions/{sid}/schedules",
        json={"prompt": "remind me about the release", "run_at": when},
        headers=HEADERS,
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["run_at"] and data["cron"] is None and data["interval_seconds"] is None
    assert data["recurrence_label"].startswith("Once on ")


@pytest.mark.asyncio
async def test_create_rejects_two_recurrences(client):
    sid = await _session(client)
    resp = await client.post(
        f"/api/sessions/{sid}/schedules",
        json={"prompt": "x", "cron": "0 9 * * *", "interval_seconds": 600},
        headers=HEADERS,
    )
    assert resp.status_code == 422
    assert "exactly one" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_create_rejects_no_recurrence(client):
    sid = await _session(client)
    resp = await client.post(
        f"/api/sessions/{sid}/schedules", json={"prompt": "x"}, headers=HEADERS
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_rejects_a_too_short_interval(client):
    """The floor is our message, not a pydantic constraint dump — the caller
    is a model that has to know what to pass instead."""
    sid = await _session(client)
    resp = await client.post(
        f"/api/sessions/{sid}/schedules",
        json={"prompt": "x", "interval_seconds": 5},
        headers=HEADERS,
    )
    assert resp.status_code == 422
    assert "60" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_create_rejects_an_unknown_timezone(client):
    sid = await _session(client)
    resp = await client.post(
        f"/api/sessions/{sid}/schedules",
        json={"prompt": "x", "cron": "0 9 * * *", "timezone": "PST"},
        headers=HEADERS,
    )
    assert resp.status_code == 422
    assert "IANA" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_create_on_an_unknown_session_is_404(client):
    resp = await client.post(
        "/api/sessions/nope/schedules",
        json={"prompt": "x", "interval_seconds": 600},
        headers=HEADERS,
    )
    assert resp.status_code == 404


# --- list ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_list_is_scoped_to_this_agent(client):
    sid = await _session(client)
    await client.post(
        f"/api/sessions/{sid}/schedules",
        json={"prompt": "mine", "interval_seconds": 600},
        headers=HEADERS,
    )
    theirs = await _other_agents_schedule(client)

    resp = await client.get(f"/api/sessions/{sid}/schedules", headers=HEADERS)
    assert resp.status_code == 200
    ids = [r["id"] for r in resp.json()]
    assert theirs not in ids
    assert len(ids) == 1
    # The unscoped admin route still sees both.
    everything = (await client.get("/api/schedules", headers=HEADERS)).json()
    assert len(everything) == 2


# --- update ---------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_pause_and_resume(client):
    sid = await _session(client)
    created = await client.post(
        f"/api/sessions/{sid}/schedules",
        json={"prompt": "x", "interval_seconds": 600},
        headers=HEADERS,
    )
    sched_id = created.json()["id"]

    paused = await client.patch(
        f"/api/sessions/{sid}/schedules/{sched_id}",
        json={"enabled": False},
        headers=HEADERS,
    )
    assert paused.status_code == 200
    assert paused.json()["enabled"] is False
    # A paused schedule has no job, so no next fire.
    assert paused.json()["next_run_at"] is None

    resumed = await client.patch(
        f"/api/sessions/{sid}/schedules/{sched_id}",
        json={"enabled": True},
        headers=HEADERS,
    )
    assert resumed.json()["enabled"] is True
    assert resumed.json()["next_run_at"]


@pytest.mark.asyncio
async def test_switching_recurrence_clears_the_old_one(client):
    """The bug this guards: an interval schedule given a cron would keep its
    interval column and fire on whichever the runner read first."""
    sid = await _session(client)
    created = await client.post(
        f"/api/sessions/{sid}/schedules",
        json={"prompt": "x", "interval_seconds": 600},
        headers=HEADERS,
    )
    sched_id = created.json()["id"]

    resp = await client.patch(
        f"/api/sessions/{sid}/schedules/{sched_id}",
        json={"cron": "0 7 * * *", "timezone": "Asia/Shanghai"},
        headers=HEADERS,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["cron"] == "0 7 * * *"
    assert data["interval_seconds"] is None
    assert data["timezone"] == "Asia/Shanghai"
    assert data["recurrence_label"] == "Every day at 07:00"

    # And it survives a reload — the columns really were rewritten.
    rows = (await client.get(f"/api/sessions/{sid}/schedules", headers=HEADERS)).json()
    assert rows[0]["interval_seconds"] is None and rows[0]["cron"] == "0 7 * * *"


@pytest.mark.asyncio
async def test_timezone_alone_moves_an_existing_cron(client):
    sid = await _session(client)
    created = await client.post(
        f"/api/sessions/{sid}/schedules",
        json={"prompt": "x", "cron": "0 9 * * *", "timezone": "America/Los_Angeles"},
        headers=HEADERS,
    )
    sched_id = created.json()["id"]
    resp = await client.patch(
        f"/api/sessions/{sid}/schedules/{sched_id}",
        json={"timezone": "Asia/Shanghai"},
        headers=HEADERS,
    )
    assert resp.status_code == 200
    assert resp.json()["cron"] == "0 9 * * *"
    assert resp.json()["timezone"] == "Asia/Shanghai"


@pytest.mark.asyncio
async def test_update_rejects_two_recurrences(client):
    sid = await _session(client)
    created = await client.post(
        f"/api/sessions/{sid}/schedules",
        json={"prompt": "x", "interval_seconds": 600},
        headers=HEADERS,
    )
    resp = await client.patch(
        f"/api/sessions/{sid}/schedules/{created.json()['id']}",
        json={"cron": "0 9 * * *", "interval_seconds": 900},
        headers=HEADERS,
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_cannot_touch_another_agents_schedule(client):
    sid = await _session(client)
    theirs = await _other_agents_schedule(client)

    patched = await client.patch(
        f"/api/sessions/{sid}/schedules/{theirs}",
        json={"enabled": False},
        headers=HEADERS,
    )
    assert patched.status_code == 404
    deleted = await client.delete(
        f"/api/sessions/{sid}/schedules/{theirs}", headers=HEADERS
    )
    assert deleted.status_code == 404
    # Still there, still running.
    everything = (await client.get("/api/schedules", headers=HEADERS)).json()
    assert [r["id"] for r in everything] == [theirs]
    assert everything[0]["enabled"] is True


# --- delete ---------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_delete(client):
    sid = await _session(client)
    created = await client.post(
        f"/api/sessions/{sid}/schedules",
        json={"prompt": "x", "interval_seconds": 600},
        headers=HEADERS,
    )
    sched_id = created.json()["id"]
    resp = await client.delete(
        f"/api/sessions/{sid}/schedules/{sched_id}", headers=HEADERS
    )
    assert resp.status_code == 204
    assert (await client.get("/api/schedules", headers=HEADERS)).json() == []
    # Gone from the runner too, not just the table.
    assert schedules_mod._runner.next_run_at(sched_id) is None


@pytest.mark.asyncio
async def test_auth_required(client):
    sid = await _session(client)
    assert (await client.get(f"/api/sessions/{sid}/schedules")).status_code in (401, 403)


# --- the change reaches open clients --------------------------------------- #


@pytest.mark.asyncio
async def test_every_mutation_broadcasts(client, monkeypatch):
    """A schedule an agent sets for itself has to show up in the UI without a
    reload — the browser only refetches when it is told the list moved."""
    seen: list[dict] = []

    async def capture(payload):
        seen.append(payload)

    monkeypatch.setattr(session_manager, "_broadcast", capture)

    sid = await _session(client)
    created = await client.post(
        f"/api/sessions/{sid}/schedules",
        json={"prompt": "x", "interval_seconds": 600},
        headers=HEADERS,
    )
    sched_id = created.json()["id"]
    await client.patch(
        f"/api/sessions/{sid}/schedules/{sched_id}",
        json={"enabled": False},
        headers=HEADERS,
    )
    await client.delete(f"/api/sessions/{sid}/schedules/{sched_id}", headers=HEADERS)

    kinds = [p["type"] for p in seen if p.get("type") == "schedules_changed"]
    assert len(kinds) == 3
