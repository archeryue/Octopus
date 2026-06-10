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

    def __init__(
        self,
        state,
        url=None,
        token=None,
        oauth_tokens=None,
        message=None,
    ):
        from server.oauth_login import LoginState

        self.id = "login-xyz"
        self.state = state
        self.url = url
        self.token = token
        self.oauth_tokens = oauth_tokens
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
async def test_oauth_start_502_on_orchestrator_runtime_error(client, monkeypatch):
    """If the orchestrator raises (e.g. an upstream network blip during
    early initialization, in some future addition), surface it as a 502
    so the client knows the gateway / upstream failed rather than a
    user-input issue."""
    from server import oauth_login

    async def fake_start(self):
        raise RuntimeError("upstream unreachable")

    monkeypatch.setattr(oauth_login.OAuthLoginManager, "start", fake_start)

    c, _ = client
    res = await c.post(
        "/api/credentials/oauth/start",
        json={"backend": "claude-code"},
        headers=AUTH,
    )
    assert res.status_code == 502
    assert "upstream unreachable" in res.json()["detail"]


@pytest.mark.asyncio
async def test_oauth_start_rejects_codex(client):
    """OAuth-redirect start is rejected for codex — its login method is
    device_code (the harness's `login.method` gates this, not a hardcoded
    backend check)."""
    c, _ = client
    res = await c.post(
        "/api/credentials/oauth/start",
        json={"backend": "codex"},
        headers=AUTH,
    )
    assert res.status_code == 400
    assert "codex" in res.json()["detail"].lower()


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
async def test_oauth_complete_persists_oauth_token_bundle(client, monkeypatch):
    """Pro/Max path: orchestrator returns oauth_tokens (no API key).
    Router should store the full bundle as JSON + populate token_expires_at."""
    import json
    import time
    from server import oauth_login
    from server.crypto import decrypt
    from server.oauth_login import LoginState
    from server.oauth_providers import OAuthTokenSet

    expires_at_epoch = time.time() + 3600
    ts = OAuthTokenSet(
        access_token="oat-fresh",
        refresh_token="ort-fresh",
        expires_at_epoch=expires_at_epoch,
        scopes=["user:inference", "user:profile"],
    )

    async def fake_submit(self, login_id, code):
        return _StubLoginSession(LoginState.success, oauth_tokens=ts)

    monkeypatch.setattr(oauth_login.OAuthLoginManager, "submit_code", fake_submit)

    c, db = client
    res = await c.post(
        "/api/credentials/oauth/complete",
        json={"login_id": "login-xyz", "code": "the-code", "label": "Pro/Max"},
        headers=AUTH,
    )
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["auth_type"] == "oauth"
    assert body["label"] == "Pro/Max"
    assert body["token_expires_at"] is not None

    rows = await db.load_credentials()
    assert len(rows) == 1
    decrypted = decrypt(rows[0]["secret_encrypted"], TOKEN)
    bundle = json.loads(decrypted)
    assert bundle["access_token"] == "oat-fresh"
    assert bundle["refresh_token"] == "ort-fresh"
    assert bundle["scopes"] == ["user:inference", "user:profile"]
    # expires_at_epoch round-trips
    assert abs(bundle["expires_at_epoch"] - expires_at_epoch) < 1
    # token_expires_at was set on the DB row
    assert rows[0]["token_expires_at"] is not None


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


# ----------------------------------------------------- re-authorization in place
# harness-credential-reauth.md §5: re-auth updates the existing credential and
# clears its needs_reconnect flag, so agent/session bindings survive.


async def _flagged_credential(db, *, cid, backend, auth_type, secret):
    from datetime import datetime, timezone
    from server.config import settings
    from server.crypto import encrypt

    await db.save_credential(
        credential_id=cid,
        backend=backend,
        label="Personal",
        auth_type=auth_type,
        secret_encrypted=encrypt(secret, settings.auth_token),
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    await db.update_credential(
        cid,
        status="needs_reconnect",
        needs_reconnect=True,
        last_refresh_error_code="invalid_credentials",
    )


@pytest.mark.asyncio
async def test_oauth_complete_reauth_updates_in_place(client, monkeypatch):
    from server import oauth_login
    from server.oauth_login import LoginState

    c, db = client
    await _flagged_credential(
        db, cid="claudecred01", backend="claude-code", auth_type="oauth",
        secret="sk-ant-old-token",
    )

    async def fake_submit(self, login_id, code):
        return _StubLoginSession(
            LoginState.success, token="sk-ant-fresh-token-1234567890"
        )

    monkeypatch.setattr(oauth_login.OAuthLoginManager, "submit_code", fake_submit)

    res = await c.post(
        "/api/credentials/oauth/complete",
        json={
            "login_id": "x", "code": "y", "label": "Personal",
            "credential_id": "claudecred01",
        },
        headers=AUTH,
    )
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["id"] == "claudecred01"          # same row, not a new one
    assert body["needs_reconnect"] is False
    assert body["status"] == "active"

    rows = await db.load_credentials()
    assert len(rows) == 1                         # no duplicate credential minted
    assert decrypt(rows[0]["secret_encrypted"], TOKEN) == "sk-ant-fresh-token-1234567890"


@pytest.mark.asyncio
async def test_oauth_complete_reauth_404_unknown_credential(client, monkeypatch):
    from server import oauth_login
    from server.oauth_login import LoginState

    async def fake_submit(self, login_id, code):
        return _StubLoginSession(LoginState.success, token="sk-ant-fresh-1234567890")

    monkeypatch.setattr(oauth_login.OAuthLoginManager, "submit_code", fake_submit)

    c, _ = client
    res = await c.post(
        "/api/credentials/oauth/complete",
        json={
            "login_id": "x", "code": "y", "label": "P", "credential_id": "nope",
        },
        headers=AUTH,
    )
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_patch_new_secret_clears_needs_reconnect(client):
    c, db = client
    await _flagged_credential(
        db, cid="apikeycred01", backend="claude-code", auth_type="api_key",
        secret="sk-ant-old",
    )
    res = await c.patch(
        "/api/credentials/apikeycred01",
        json={"secret": "sk-ant-fresh-key"},
        headers=AUTH,
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["needs_reconnect"] is False
    assert body["status"] == "active"
    rows = await db.load_credentials()
    assert decrypt(rows[0]["secret_encrypted"], TOKEN) == "sk-ant-fresh-key"


@pytest.mark.asyncio
async def test_codex_status_reauth_clears_flag_in_place(client):
    from server.codex_login import (
        CodexLoginSession,
        CodexLoginState,
        codex_login_manager,
    )

    c, db = client
    await _flagged_credential(
        db, cid="codexcred001", backend="codex", auth_type="oauth",
        secret="/tmp/codexhome",
    )
    # A successful re-auth login whose credential_id is the existing row's id
    # (codex re-ran into the same CODEX_HOME). Inject it into the singleton.
    sess = CodexLoginSession(
        id="login-reauth",
        credential_id="codexcred001",
        codex_home="/tmp/codexhome",
        label="Personal",
        state=CodexLoginState.success,
    )
    codex_login_manager._sessions["login-reauth"] = sess
    try:
        res = await c.get(
            "/api/credentials/codex/login-reauth/status", headers=AUTH
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["state"] == "success"
        assert body["credential"]["id"] == "codexcred001"
        assert body["credential"]["needs_reconnect"] is False

        rows = await db.load_credentials()
        assert len(rows) == 1                     # still one row, updated in place
        row = await db.get_credential("codexcred001")
        assert row["needs_reconnect"] is False
        assert row["status"] == "active"
    finally:
        codex_login_manager._sessions.pop("login-reauth", None)
