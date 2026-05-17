"""Tests for the /api/credentials REST endpoints."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from server.crypto import decrypt
from server.database import Database
from server.main import app
from server.routers import credentials as creds_router

TOKEN = "changeme"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
async def client():
    db = Database(":memory:")
    await db.initialize()
    creds_router.set_db(db)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c, db
    await db.close()
    creds_router.set_db(None)


@pytest.mark.asyncio
async def test_create_returns_metadata_not_secret(client):
    c, db = client
    res = await c.post(
        "/api/credentials",
        json={
            "backend": "claude-code",
            "label": "Personal",
            "auth_type": "api_key",
            "secret": "sk-ant-abc",
        },
        headers=AUTH,
    )
    assert res.status_code == 201
    body = res.json()
    assert body["label"] == "Personal"
    assert body["backend"] == "claude-code"
    assert "secret" not in body
    assert "secret_encrypted" not in body
    # Confirm the secret really was stored (encrypted)
    rows = await db.load_credentials()
    assert len(rows) == 1
    assert decrypt(rows[0]["secret_encrypted"], TOKEN) == "sk-ant-abc"


@pytest.mark.asyncio
async def test_list_omits_secret(client):
    c, _ = client
    await c.post(
        "/api/credentials",
        json={"backend": "claude-code", "label": "L1", "secret": "x1"},
        headers=AUTH,
    )
    await c.post(
        "/api/credentials",
        json={"backend": "codex", "label": "L2", "secret": "x2"},
        headers=AUTH,
    )
    res = await c.get("/api/credentials", headers=AUTH)
    assert res.status_code == 200
    items = res.json()
    assert len(items) == 2
    for item in items:
        assert "secret" not in item
        assert "secret_encrypted" not in item
    assert {i["label"] for i in items} == {"L1", "L2"}


@pytest.mark.asyncio
async def test_update_renames_and_rotates_secret(client):
    c, db = client
    create = await c.post(
        "/api/credentials",
        json={"backend": "claude-code", "label": "Old", "secret": "old"},
        headers=AUTH,
    )
    cid = create.json()["id"]
    # Rename only
    res = await c.patch(
        f"/api/credentials/{cid}",
        json={"label": "New"},
        headers=AUTH,
    )
    assert res.status_code == 200
    assert res.json()["label"] == "New"
    # Rotate secret
    res = await c.patch(
        f"/api/credentials/{cid}",
        json={"secret": "rotated"},
        headers=AUTH,
    )
    assert res.status_code == 200
    row = await db.get_credential(cid)
    assert decrypt(row["secret_encrypted"], TOKEN) == "rotated"
    assert row["label"] == "New"  # unchanged


@pytest.mark.asyncio
async def test_update_unknown_returns_404(client):
    c, _ = client
    res = await c.patch(
        "/api/credentials/missing", json={"label": "x"}, headers=AUTH
    )
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_delete_then_404(client):
    c, _ = client
    create = await c.post(
        "/api/credentials",
        json={"backend": "claude-code", "label": "L", "secret": "x"},
        headers=AUTH,
    )
    cid = create.json()["id"]
    assert (await c.delete(f"/api/credentials/{cid}", headers=AUTH)).status_code == 204
    assert (await c.delete(f"/api/credentials/{cid}", headers=AUTH)).status_code == 404


@pytest.mark.asyncio
async def test_rejects_unauthenticated(client):
    c, _ = client
    res = await c.get("/api/credentials")
    assert res.status_code in (401, 403)
    res = await c.post(
        "/api/credentials",
        json={"backend": "claude-code", "label": "L", "secret": "x"},
    )
    assert res.status_code in (401, 403)


@pytest.mark.asyncio
async def test_create_rejects_empty_secret(client):
    c, _ = client
    res = await c.post(
        "/api/credentials",
        json={"backend": "claude-code", "label": "L", "secret": ""},
        headers=AUTH,
    )
    assert res.status_code == 422  # pydantic validation


# ---------------------------------------------------------------------------
# OAuth (in-app subscription login)
# ---------------------------------------------------------------------------


class _StubLoginSession:
    """Stand-in for server.oauth_login.LoginSession that the orchestrator
    would have produced. Lets the REST tests run without spawning a real
    `claude` subprocess."""

    def __init__(self, state, url=None, token=None, message=None):
        from server.oauth_login import LoginState

        self.id = "login-xyz"
        self.state = state
        self.url = url
        self.token = token
        self.message = message
        # The real LoginSession has more fields the router doesn't read;
        # we deliberately omit them to keep the stub small.

    def __getattr__(self, name):  # pragma: no cover — defensive
        return None


@pytest.mark.asyncio
async def test_oauth_start_returns_login_id_and_url(client, monkeypatch):
    from server import oauth_login
    from server.oauth_login import LoginState

    async def fake_start(self):
        return _StubLoginSession(LoginState.awaiting_code, url="https://claude.ai/oauth/authorize?fake")

    monkeypatch.setattr(oauth_login.OAuthLoginManager, "start", fake_start)

    c, _ = client
    res = await c.post(
        "/api/credentials/oauth/start",
        json={"backend": "claude-code"},
        headers=AUTH,
    )
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["login_id"] == "login-xyz"
    assert body["device_url"].startswith("https://claude.ai/oauth/authorize")


@pytest.mark.asyncio
async def test_oauth_start_503_when_binary_missing(client, monkeypatch):
    from server import oauth_login

    async def fake_start(self):
        raise FileNotFoundError("claude CLI not found on PATH")

    monkeypatch.setattr(oauth_login.OAuthLoginManager, "start", fake_start)

    c, _ = client
    res = await c.post(
        "/api/credentials/oauth/start",
        json={"backend": "claude-code"},
        headers=AUTH,
    )
    assert res.status_code == 503


@pytest.mark.asyncio
async def test_oauth_start_rejects_codex_for_now(client):
    c, _ = client
    res = await c.post(
        "/api/credentials/oauth/start",
        json={"backend": "codex"},
        headers=AUTH,
    )
    assert res.status_code == 400
    assert "claude-code" in res.json()["detail"]


@pytest.mark.asyncio
async def test_oauth_complete_persists_credential(client, monkeypatch):
    from server import oauth_login
    from server.crypto import decrypt
    from server.oauth_login import LoginState

    async def fake_submit(self, login_id, code):
        assert login_id == "login-xyz"
        assert code == "the-code"
        return _StubLoginSession(
            LoginState.success, token="sk-ant-fake-oauth-token-abcdef1234567890"
        )

    monkeypatch.setattr(oauth_login.OAuthLoginManager, "submit_code", fake_submit)

    c, db = client
    res = await c.post(
        "/api/credentials/oauth/complete",
        json={"login_id": "login-xyz", "code": "the-code", "label": "Personal"},
        headers=AUTH,
    )
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["label"] == "Personal"
    assert body["auth_type"] == "oauth"
    assert body["backend"] == "claude-code"
    assert "secret" not in body  # never leaks the token

    rows = await db.load_credentials()
    assert len(rows) == 1
    assert decrypt(rows[0]["secret_encrypted"], TOKEN).startswith("sk-ant-fake-oauth")


@pytest.mark.asyncio
async def test_oauth_complete_returns_500_on_no_token(client, monkeypatch):
    from server import oauth_login
    from server.oauth_login import LoginState

    async def fake_submit(self, login_id, code):
        return _StubLoginSession(LoginState.error, message="token exchange failed")

    monkeypatch.setattr(oauth_login.OAuthLoginManager, "submit_code", fake_submit)

    c, _ = client
    res = await c.post(
        "/api/credentials/oauth/complete",
        json={"login_id": "login-xyz", "code": "bad-code", "label": "L"},
        headers=AUTH,
    )
    assert res.status_code == 500


@pytest.mark.asyncio
async def test_oauth_complete_404_unknown_id(client, monkeypatch):
    from server import oauth_login

    async def fake_submit(self, login_id, code):
        raise KeyError(login_id)

    monkeypatch.setattr(oauth_login.OAuthLoginManager, "submit_code", fake_submit)

    c, _ = client
    res = await c.post(
        "/api/credentials/oauth/complete",
        json={"login_id": "ghost", "code": "x", "label": "L"},
        headers=AUTH,
    )
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_oauth_cancel_is_idempotent(client, monkeypatch):
    from server import oauth_login

    called: list[str] = []

    async def fake_cancel(self, login_id):
        called.append(login_id)

    monkeypatch.setattr(oauth_login.OAuthLoginManager, "cancel", fake_cancel)

    c, _ = client
    res = await c.post(
        "/api/credentials/oauth/cancel",
        json={"login_id": "login-xyz"},
        headers=AUTH,
    )
    assert res.status_code == 204
    res = await c.post(
        "/api/credentials/oauth/cancel",
        json={"login_id": "login-xyz"},
        headers=AUTH,
    )
    assert res.status_code == 204
    assert called == ["login-xyz", "login-xyz"]


@pytest.mark.asyncio
async def test_oauth_endpoints_require_auth(client):
    c, _ = client
    for path, body in [
        ("/api/credentials/oauth/start", {"backend": "claude-code"}),
        (
            "/api/credentials/oauth/complete",
            {"login_id": "x", "code": "y", "label": "L"},
        ),
        ("/api/credentials/oauth/cancel", {"login_id": "x"}),
    ]:
        res = await c.post(path, json=body)
        assert res.status_code in (401, 403), (path, res.status_code)
