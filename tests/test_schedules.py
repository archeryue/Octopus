"""Tests for schedules API endpoints."""

import pytest
from httpx import ASGITransport, AsyncClient

from server.database import Database
from server.main import app
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

    runner = ScheduleRunner(session_manager, db)
    await runner.initialize()
    schedules_mod._db = db
    schedules_mod._runner = runner

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c

    await runner.shutdown()
    await db.close()


async def _create_session(client):
    resp = await client.post(
        "/api/sessions", json={"name": "test"}, headers=HEADERS
    )
    assert resp.status_code == 201
    return resp.json()["id"]


@pytest.mark.asyncio
async def test_list_schedules_empty(client):
    resp = await client.get("/api/schedules", headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_create_schedule(client):
    session_id = await _create_session(client)
    resp = await client.post(
        "/api/schedules",
        json={
            "session_id": session_id,
            "name": "daily check",
            "prompt": "check status",
            "interval_seconds": 60,
        },
        headers=HEADERS,
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "daily check"
    assert data["prompt"] == "check status"
    assert data["interval_seconds"] == 60
    assert data["enabled"] is True
    # Schedules are agent-scoped now: the legacy session_id resolved to the
    # session's owning agent (agent-refactor.md §5.4).
    sess = (await client.get(f"/api/sessions/{session_id}", headers=HEADERS)).json()
    assert data["agent_id"] == sess["agent_id"]


@pytest.mark.asyncio
async def test_create_schedule_invalid_session(client):
    resp = await client.post(
        "/api/schedules",
        json={
            "session_id": "nonexistent",
            "name": "test",
            "prompt": "hello",
            "interval_seconds": 60,
        },
        headers=HEADERS,
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_create_schedule_interval_too_short(client):
    session_id = await _create_session(client)
    resp = await client.post(
        "/api/schedules",
        json={
            "session_id": session_id,
            "name": "test",
            "prompt": "hello",
            "interval_seconds": 30,
        },
        headers=HEADERS,
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_list_schedules_after_create(client):
    session_id = await _create_session(client)
    await client.post(
        "/api/schedules",
        json={
            "session_id": session_id,
            "name": "sched1",
            "prompt": "hi",
            "interval_seconds": 120,
        },
        headers=HEADERS,
    )
    resp = await client.get("/api/schedules", headers=HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["name"] == "sched1"


@pytest.mark.asyncio
async def test_update_schedule(client):
    session_id = await _create_session(client)
    create_resp = await client.post(
        "/api/schedules",
        json={
            "session_id": session_id,
            "name": "original",
            "prompt": "hi",
            "interval_seconds": 60,
        },
        headers=HEADERS,
    )
    sched_id = create_resp.json()["id"]

    resp = await client.patch(
        f"/api/schedules/{sched_id}",
        json={"name": "updated", "enabled": False},
        headers=HEADERS,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "updated"
    assert data["enabled"] is False


@pytest.mark.asyncio
async def test_update_schedule_not_found(client):
    resp = await client.patch(
        "/api/schedules/nonexistent",
        json={"name": "x"},
        headers=HEADERS,
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_schedule(client):
    session_id = await _create_session(client)
    create_resp = await client.post(
        "/api/schedules",
        json={
            "session_id": session_id,
            "name": "to_delete",
            "prompt": "hi",
            "interval_seconds": 60,
        },
        headers=HEADERS,
    )
    sched_id = create_resp.json()["id"]

    resp = await client.delete(f"/api/schedules/{sched_id}", headers=HEADERS)
    assert resp.status_code == 204

    list_resp = await client.get("/api/schedules", headers=HEADERS)
    assert list_resp.json() == []


@pytest.mark.asyncio
async def test_auth_required(client):
    resp = await client.get("/api/schedules")
    assert resp.status_code in (401, 403)
