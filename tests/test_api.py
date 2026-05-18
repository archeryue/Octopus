"""End-to-end tests for REST API using FastAPI TestClient."""

import pytest
from httpx import ASGITransport, AsyncClient

from server.database import Database
from server.main import app
from server.session_manager import session_manager

TOKEN = "changeme"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
async def client():
    # Initialize session_manager with in-memory DB before each test
    db = Database(":memory:")
    await db.initialize()
    session_manager.sessions.clear()
    await session_manager.initialize(db)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c

    await db.close()


@pytest.mark.asyncio
async def test_health(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"


@pytest.mark.asyncio
async def test_auth_required(client):
    resp = await client.get("/api/sessions")
    assert resp.status_code in (401, 403)


@pytest.mark.asyncio
async def test_auth_bad_token(client):
    resp = await client.get(
        "/api/sessions", headers={"Authorization": "Bearer wrong"}
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_list_sessions_empty(client):
    resp = await client.get("/api/sessions", headers=HEADERS)
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


@pytest.mark.asyncio
async def test_create_session(client):
    resp = await client.post(
        "/api/sessions",
        headers=HEADERS,
        json={"name": "Test Session", "working_dir": "/tmp"},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "Test Session"
    assert data["working_dir"] == "/tmp"
    assert data["status"] == "idle"
    assert "id" in data


@pytest.mark.asyncio
async def test_get_session(client):
    # Create first
    create_resp = await client.post(
        "/api/sessions",
        headers=HEADERS,
        json={"name": "Get Me"},
    )
    sid = create_resp.json()["id"]

    # Get it
    resp = await client.get(f"/api/sessions/{sid}", headers=HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == sid
    assert data["name"] == "Get Me"
    assert "messages" in data


@pytest.mark.asyncio
async def test_get_session_not_found(client):
    resp = await client.get("/api/sessions/nonexistent", headers=HEADERS)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_session(client):
    create_resp = await client.post(
        "/api/sessions",
        headers=HEADERS,
        json={"name": "Delete Me"},
    )
    sid = create_resp.json()["id"]

    resp = await client.delete(f"/api/sessions/{sid}", headers=HEADERS)
    assert resp.status_code == 204

    # Verify gone
    resp = await client.get(f"/api/sessions/{sid}", headers=HEADERS)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_session_not_found(client):
    resp = await client.delete("/api/sessions/nonexistent", headers=HEADERS)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_archive_session(client):
    """POST /api/sessions/{id}/archive returns a fresh SessionInfo with the
    same name/working_dir, the old session disappears from the list,
    and the new id is different."""
    create_resp = await client.post(
        "/api/sessions",
        headers=HEADERS,
        json={"name": "Archive Me", "working_dir": "/tmp/archived"},
    )
    old_id = create_resp.json()["id"]

    arc = await client.post(
        f"/api/sessions/{old_id}/archive", headers=HEADERS
    )
    assert arc.status_code == 201
    body = arc.json()
    new_id = body["id"]
    assert new_id != old_id
    assert body["name"] == "Archive Me"
    assert body["working_dir"] == "/tmp/archived"

    # Old session is hidden from the list; new one appears.
    list_resp = await client.get("/api/sessions", headers=HEADERS)
    ids = [s["id"] for s in list_resp.json()]
    assert old_id not in ids
    assert new_id in ids

    # GET on the old id still works — it returns the archived row's
    # detail (so the UI's "view archived" can read history).
    archived = await client.get(f"/api/sessions/{old_id}", headers=HEADERS)
    assert archived.status_code == 200
    assert archived.json()["archived"] is True

    # GET on the list with ?include_archived=true surfaces both.
    inc = await client.get(
        "/api/sessions?include_archived=true", headers=HEADERS
    )
    ids = [s["id"] for s in inc.json()]
    assert old_id in ids
    assert new_id in ids

    # Unarchive brings the old id back; it returns to the default list.
    un = await client.post(
        f"/api/sessions/{old_id}/unarchive", headers=HEADERS
    )
    assert un.status_code == 200
    assert un.json()["id"] == old_id
    assert un.json()["archived"] is False
    list_after = await client.get("/api/sessions", headers=HEADERS)
    assert old_id in [s["id"] for s in list_after.json()]


@pytest.mark.asyncio
async def test_archive_session_not_found(client):
    resp = await client.post(
        "/api/sessions/nonexistent/archive", headers=HEADERS
    )
    assert resp.status_code == 404
