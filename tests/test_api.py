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


@pytest.mark.asyncio
async def test_create_session_defaults_backend_to_claude_code(client):
    resp = await client.post(
        "/api/sessions", headers=HEADERS, json={"name": "Default Backend"}
    )
    assert resp.status_code == 201
    assert resp.json()["backend"] == "claude-code"


@pytest.mark.asyncio
async def test_create_session_with_codex_backend(client):
    resp = await client.post(
        "/api/sessions", headers=HEADERS, json={"name": "Cx", "backend": "codex"}
    )
    assert resp.status_code == 201
    assert resp.json()["backend"] == "codex"


@pytest.mark.asyncio
async def test_create_session_rejects_credential_backend_mismatch(client):
    """A Codex session must not run a claude-code credential (codex-backend.md
    §4.2) — the route returns 400."""
    from datetime import datetime, timezone
    from server.config import settings
    from server.crypto import encrypt

    enc = encrypt("sk-x", settings.auth_token)
    await session_manager.db.save_credential(
        credential_id="c-cc",
        backend="claude-code",
        label="L",
        auth_type="api_key",
        secret_encrypted=enc,
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    resp = await client.post(
        "/api/sessions",
        headers=HEADERS,
        json={"name": "Bad", "backend": "codex", "credential_id": "c-cc"},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_list_backends_includes_claude_code(client):
    resp = await client.get("/api/backends", headers=HEADERS)
    assert resp.status_code == 200
    assert "claude-code" in resp.json()["available"]


@pytest.mark.asyncio
async def test_session_info_reports_the_steering_capability(client):
    """`can_steer` is a harness capability, derived like `can_fork` — the
    composer reads it to say what the send button will actually do. Claiming
    "Queue message" when the message will steer is worse than saying nothing.
    """
    from server.routers.sessions import _can_steer

    # Claude takes input on stdin, so a turn on it can be steered mid-flight.
    assert _can_steer("claude-code") is True
    # Codex's prompt lives in argv; it keeps queue-until-idle.
    assert _can_steer("codex") is False
    # An unknown backend must not claim a capability it can't honour.
    assert _can_steer("nonsense-backend") is False

    res = await client.post(
        "/api/sessions", headers=HEADERS, json={"name": "Steerable"}
    )
    assert res.status_code == 201
    assert res.json()["can_steer"] is True

    cx = await client.post(
        "/api/sessions", headers=HEADERS, json={"name": "Cx", "backend": "codex"}
    )
    assert cx.status_code == 201
    assert cx.json()["can_steer"] is False


@pytest.mark.asyncio
async def test_a_404_is_never_cacheable(client):
    """We send no Cache-Control on a 404, so a CDN invents one — Cloudflare
    caches error responses for static-looking paths for four hours. The first
    request for an asset that doesn't exist YET then poisons that URL long
    after the file lands: the origin serves it, everyone behind the edge sees
    a 404. That cost real debugging time on an apple-touch-icon.
    """
    # Static-looking extensions specifically: those are what a CDN caches.
    for path in ("/not-here.png", "/nope.js", "/api/definitely-not-a-route"):
        resp = await client.get(path, headers=HEADERS)
        assert resp.status_code == 404, path
        assert resp.headers.get("cache-control") == "no-store", path


@pytest.mark.asyncio
async def test_a_successful_response_keeps_its_own_cache_headers(client):
    """The 404 rule must not reach into anything else."""
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.headers.get("cache-control") != "no-store"
