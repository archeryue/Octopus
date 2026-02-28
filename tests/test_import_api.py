"""Tests for the POST /api/sessions/import endpoint."""

import pytest
from httpx import ASGITransport, AsyncClient

from server.database import Database
from server.main import app
from server.session_manager import session_manager

TOKEN = "changeme"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
async def client():
    db = Database(":memory:")
    await db.initialize()
    session_manager.sessions.clear()
    await session_manager.initialize(db)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c

    await db.close()


@pytest.mark.asyncio
async def test_import_basic(client):
    resp = await client.post(
        "/api/sessions/import",
        headers=HEADERS,
        json={
            "name": "Imported",
            "messages": [
                {"role": "user", "type": "text", "content": "hello"},
                {"role": "assistant", "type": "text", "content": "hi there"},
            ],
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "Imported"
    assert data["message_count"] == 2
    assert len(data["messages"]) == 2


@pytest.mark.asyncio
async def test_import_sets_claude_session_id(client):
    resp = await client.post(
        "/api/sessions/import",
        headers=HEADERS,
        json={
            "name": "With Resume",
            "claude_session_id": "abc-123-def",
            "messages": [{"role": "user", "type": "text", "content": "test"}],
        },
    )
    assert resp.status_code == 201
    sid = resp.json()["id"]

    # Verify via GET that the session has the claude_session_id
    # (it's stored on the Session object; we can check the manager directly)
    session = session_manager.get_session(sid)
    assert session is not None
    assert session.claude_session_id == "abc-123-def"


@pytest.mark.asyncio
async def test_import_messages_retrievable(client):
    resp = await client.post(
        "/api/sessions/import",
        headers=HEADERS,
        json={
            "name": "Persistent",
            "messages": [
                {"role": "user", "type": "text", "content": "msg1"},
                {
                    "role": "assistant",
                    "type": "tool_use",
                    "tool_name": "Read",
                    "tool_input": {"path": "/f"},
                    "tool_use_id": "t1",
                },
                {
                    "role": "tool",
                    "type": "tool_result",
                    "content": "file data",
                    "tool_use_id": "t1",
                },
            ],
        },
    )
    sid = resp.json()["id"]

    # GET the session and verify messages are persisted
    get_resp = await client.get(f"/api/sessions/{sid}", headers=HEADERS)
    assert get_resp.status_code == 200
    messages = get_resp.json()["messages"]
    assert len(messages) == 3
    assert messages[0]["content"] == "msg1"
    assert messages[1]["tool_name"] == "Read"
    assert messages[2]["type"] == "tool_result"


@pytest.mark.asyncio
async def test_import_auth_required(client):
    resp = await client.post(
        "/api/sessions/import",
        json={"name": "No Auth", "messages": []},
    )
    assert resp.status_code in (401, 403)


@pytest.mark.asyncio
async def test_import_empty_messages(client):
    resp = await client.post(
        "/api/sessions/import",
        headers=HEADERS,
        json={"name": "Empty"},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["message_count"] == 0
    assert data["messages"] == []


@pytest.mark.asyncio
async def test_import_appears_in_list(client):
    await client.post(
        "/api/sessions/import",
        headers=HEADERS,
        json={
            "name": "Listed Session",
            "messages": [{"role": "user", "type": "text", "content": "hey"}],
        },
    )

    resp = await client.get("/api/sessions", headers=HEADERS)
    assert resp.status_code == 200
    sessions = resp.json()
    names = [s["name"] for s in sessions]
    assert "Listed Session" in names
