"""Tests for in-app Codex device-auth login (server/codex_login.py + routes).

The real `codex login --device-auth` is replaced by a fake CLI fixture, so
these run anywhere (no codex binary, no ChatGPT account). The fake emits the
same URL + code shape and writes auth.json on the success path."""

import os
import sys

import pytest
from httpx import ASGITransport, AsyncClient

from server import codex_login
from server.config import settings
from server.database import Database
from server.main import app
from server.routers import credentials as credentials_mod

TOKEN = "changeme"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}

_FAKE = os.path.join(os.path.dirname(__file__), "_fixtures", "fake_codex_login.py")


@pytest.fixture(autouse=True)
def _isolate_codex_home(tmp_path, monkeypatch):
    """Point CODEX_HOME root at a temp dir + route the manager at the fake CLI."""
    monkeypatch.setattr(settings, "codex_home_dir", str(tmp_path / "codex"))
    monkeypatch.setattr(
        codex_login,
        "build_codex_login_argv",
        lambda: [sys.executable, _FAKE, "login", "--device-auth"],
    )
    # Fresh manager state each test.
    codex_login.codex_login_manager._sessions.clear()
    monkeypatch.delenv("CODEX_FAKE_LOGIN_MODE", raising=False)
    yield


@pytest.fixture
async def client():
    db = Database(":memory:")
    await db.initialize()
    credentials_mod.set_db(db)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c, db
    await db.close()


# --- manager unit tests ----------------------------------------------------


@pytest.mark.asyncio
async def test_start_scrapes_url_and_code():
    mgr = codex_login.CodexLoginManager()
    session = await mgr.start("My ChatGPT")
    # `start` returns immediately (non-blocking); the URL+code are scraped by
    # the drive task. Wait for it, then assert what was captured.
    assert os.path.isdir(session.codex_home)
    await session._task
    assert session.verification_url == "https://auth.openai.com/codex/device"
    assert session.user_code == "TEST-CODE9"


@pytest.mark.asyncio
async def test_success_writes_authjson_and_marks_success():
    mgr = codex_login.CodexLoginManager()
    session = await mgr.start("ok")
    await session._task
    assert session.state == codex_login.CodexLoginState.success
    assert os.path.exists(os.path.join(session.codex_home, "auth.json"))


@pytest.mark.asyncio
async def test_failure_marks_error_and_cleans_dir(monkeypatch):
    monkeypatch.setenv("CODEX_FAKE_LOGIN_MODE", "fail")
    mgr = codex_login.CodexLoginManager()
    session = await mgr.start("bad")
    await session._task
    assert session.state == codex_login.CodexLoginState.error
    assert not os.path.exists(session.codex_home)


@pytest.mark.asyncio
async def test_cancel_kills_and_cleans(monkeypatch):
    monkeypatch.setenv("CODEX_FAKE_LOGIN_MODE", "hang")
    mgr = codex_login.CodexLoginManager()
    session = await mgr.start("hang")  # returns after scrape; proc still running
    assert session.state == codex_login.CodexLoginState.pending
    await mgr.cancel(session.id)
    assert session.state == codex_login.CodexLoginState.cancelled
    assert not os.path.exists(session.codex_home)


# --- route tests -----------------------------------------------------------


@pytest.mark.asyncio
async def test_codex_login_route_end_to_end(client):
    c, db = client
    start = await c.post(
        "/api/credentials/codex/start", json={"label": "ChatGPT Plus"}, headers=HEADERS
    )
    assert start.status_code == 201, start.text
    login_id = start.json()["login_id"]

    # Poll until the fake writes auth.json and the route persists the credential.
    # The URL + code surface via status (start is non-blocking).
    cred = None
    saw_code = False
    for _ in range(50):
        st = await c.get(f"/api/credentials/codex/{login_id}/status", headers=HEADERS)
        assert st.status_code == 200
        data = st.json()
        if data.get("verification_url") and data.get("user_code"):
            saw_code = True
            assert data["verification_url"].startswith("https://")
        if data["state"] == "success":
            cred = data["credential"]
            break
        assert data["state"] != "error", data
        import asyncio

        await asyncio.sleep(0.05)
    assert saw_code, "URL + code never surfaced via status"
    assert cred is not None, "login never reached success"
    assert cred["backend"] == "codex"
    assert cred["auth_type"] == "oauth"

    # It shows up in the credential list, scoped to the codex backend.
    listed = (await c.get("/api/credentials", headers=HEADERS)).json()
    assert any(x["id"] == cred["id"] and x["backend"] == "codex" for x in listed)

    # Deleting it removes the CODEX_HOME dir.
    home = codex_login.codex_home_for(cred["id"])
    assert os.path.exists(os.path.join(home, "auth.json"))
    delr = await c.delete(f"/api/credentials/{cred['id']}", headers=HEADERS)
    assert delr.status_code == 204
    assert not os.path.exists(home)


@pytest.mark.asyncio
async def test_codex_login_status_unknown(client):
    c, _ = client
    r = await c.get("/api/credentials/codex/nope/status", headers=HEADERS)
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_codex_home_for_resolution():
    """`_codex_home_for` resolves the per-credential dir deterministically and
    only when it's a completed login (auth.json present)."""
    from types import SimpleNamespace

    from server.session_manager import SessionManager

    mgr = SessionManager()
    sess = SimpleNamespace(credential_id="credX", id="s1")

    # No dir yet → fall back to host login.
    assert mgr._codex_home_for(sess) is None

    home = codex_login.codex_home_for("credX")
    os.makedirs(home, exist_ok=True)
    # Dir exists but no auth.json (interrupted login) → still None.
    assert mgr._codex_home_for(sess) is None

    open(os.path.join(home, "auth.json"), "w").close()
    assert mgr._codex_home_for(sess) == home

    # Falls back to the agent's default credential when the session has none.
    no_cred = SimpleNamespace(credential_id=None, id="s2")
    assert mgr._codex_home_for(no_cred, {"credential_id": "credX"}) == home
    # No credential anywhere → None.
    assert mgr._codex_home_for(no_cred, None) is None


@pytest.mark.asyncio
async def test_codex_login_cancel_route(client, monkeypatch):
    monkeypatch.setenv("CODEX_FAKE_LOGIN_MODE", "hang")
    c, _ = client
    start = await c.post(
        "/api/credentials/codex/start", json={"label": "x"}, headers=HEADERS
    )
    login_id = start.json()["login_id"]
    cancel = await c.post(
        "/api/credentials/codex/cancel", json={"login_id": login_id}, headers=HEADERS
    )
    assert cancel.status_code == 204
    st = await c.get(f"/api/credentials/codex/{login_id}/status", headers=HEADERS)
    assert st.json()["state"] == "cancelled"
