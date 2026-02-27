"""End-to-end tests for REST API using FastAPI TestClient."""

import pytest
from httpx import ASGITransport, AsyncClient

from server.main import app

TOKEN = "changeme"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_health(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


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
