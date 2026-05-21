"""Tests for the /api/connectors routes (connectors.md §5.5 / §8): auth,
catalog, the OAuth start→callback→status install flow, installation CRUD, the
internal token route, and the agent-scoped enable routes."""

from __future__ import annotations

import time

import pytest
from httpx import ASGITransport, AsyncClient

from server.agent_manager import AgentManager
from server.connector_manager import ConnectorManager
from server.connectors.base import ConnectorBase
from server.connectors.registry import KIND_REGISTRY, register
from server.database import Database
from server.main import app
from server.oauth_providers import OAuthTokenSet
from server.routers import agents as agents_mod
from server.routers import connectors as connectors_mod
from server.session_manager import session_manager

TOKEN = "changeme"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


class FakeProvider:
    kind = "fakekind"
    authorize_url = "https://fake/auth"
    token_url = "https://fake/token"
    default_scopes = ["s"]
    pkce = True
    client_id = "cid"
    client_secret = "csec"

    def build_authorize_url(self, *, redirect_uri, code_challenge, state):
        return f"{self.authorize_url}?state={state}"

    async def exchange_code(self, *, code, redirect_uri, code_verifier, state):
        return OAuthTokenSet("at-" + code, "rt", time.time() + 3600, scopes=["s"])

    async def refresh(self, refresh_token):
        return OAuthTokenSet("at2", refresh_token, time.time() + 3600)


class FakeConnector(ConnectorBase):
    kind = "fakekind"
    display_name = "Fake"
    category = "test"
    allows_multiple = True
    oauth = FakeProvider()
    tools = ("search",)

    async def fetch_external_identity(self, token_set):
        return ("ext-1", "me@example.com")


class UnconfiguredProvider:
    kind = "noconfig"
    pkce = False


class UnconfiguredConnector(ConnectorBase):
    kind = "noconfig"
    display_name = "NoConfig"
    oauth = UnconfiguredProvider()


@pytest.fixture
async def client():
    db = Database(":memory:")
    await db.initialize()
    session_manager.sessions.clear()
    await session_manager.initialize(db)
    agents_mod.set_manager(AgentManager(db))
    connectors_mod.set_manager(ConnectorManager(db))

    saved = dict(KIND_REGISTRY)
    KIND_REGISTRY.clear()
    register(FakeConnector())

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c

    KIND_REGISTRY.clear()
    KIND_REGISTRY.update(saved)
    await db.close()


async def _start(client, kind="fakekind", **body):
    r = await client.post(
        "/api/connectors/oauth/start", json={"kind": kind, **body}, headers=HEADERS
    )
    return r


async def _install(client, code="abc"):
    """Run the full OAuth flow and return the created installation dict."""
    r = await _start(client)
    assert r.status_code == 201, r.text
    login_id = r.json()["login_id"]
    raw_state = connectors_mod._login_mgr.get(login_id).state
    cb = await client.get(
        "/api/connectors/oauth/callback",
        params={"code": code, "state": f"{login_id}:{raw_state}"},
    )
    assert cb.status_code == 200
    insts = (await client.get("/api/connectors", headers=HEADERS)).json()
    return insts[-1], login_id


async def _new_agent(client, name="Researcher"):
    r = await client.post("/api/agents", json={"name": name}, headers=HEADERS)
    assert r.status_code == 201, r.text
    return r.json()["id"]


# --- auth + catalog --------------------------------------------------------


@pytest.mark.asyncio
async def test_requires_auth(client):
    r = await client.get("/api/connectors")
    assert r.status_code in (401, 403)


@pytest.mark.asyncio
async def test_catalog_lists_registered_kinds(client):
    r = await client.get("/api/connectors/catalog", headers=HEADERS)
    assert r.status_code == 200
    cat = {c["kind"]: c for c in r.json()}
    assert cat["fakekind"]["available"] is True
    assert cat["fakekind"]["display_name"] == "Fake"


# --- OAuth install flow ----------------------------------------------------


@pytest.mark.asyncio
async def test_oauth_start_unknown_kind(client):
    r = await _start(client, kind="ghost")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_oauth_start_unconfigured_kind(client):
    register(UnconfiguredConnector())
    r = await _start(client, kind="noconfig")
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_oauth_flow_installs_and_reports_success(client):
    inst, login_id = await _install(client)
    assert inst["kind"] == "fakekind"
    assert inst["label"] == "me@example.com"
    assert inst["external_account_id"] == "ext-1"
    # Status poll reflects success + the new installation id.
    st = await client.get(
        f"/api/connectors/oauth/status/{login_id}", headers=HEADERS
    )
    assert st.status_code == 200
    assert st.json()["status"] == "success"
    assert st.json()["installation_id"] == inst["id"]


@pytest.mark.asyncio
async def test_oauth_callback_rejects_bad_state(client):
    r = await _start(client)
    login_id = r.json()["login_id"]
    cb = await client.get(
        "/api/connectors/oauth/callback",
        params={"code": "abc", "state": f"{login_id}:tampered"},
    )
    assert cb.status_code == 200  # HTML page either way
    assert "failed" in cb.text.lower()
    # Nothing got installed.
    assert (await client.get("/api/connectors", headers=HEADERS)).json() == []


@pytest.mark.asyncio
async def test_oauth_status_unknown_login(client):
    r = await client.get("/api/connectors/oauth/status/nope", headers=HEADERS)
    assert r.status_code == 404


# --- installation management ----------------------------------------------


@pytest.mark.asyncio
async def test_patch_and_delete_installation(client):
    inst, _ = await _install(client)
    iid = inst["id"]
    p = await client.patch(
        f"/api/connectors/{iid}",
        json={"label": "Work", "enable_by_default": True},
        headers=HEADERS,
    )
    assert p.status_code == 200
    assert p.json()["label"] == "Work"
    assert p.json()["enable_by_default"] is True

    d = await client.delete(f"/api/connectors/{iid}", headers=HEADERS)
    assert d.status_code == 204
    # Second delete 404s.
    assert (await client.delete(f"/api/connectors/{iid}", headers=HEADERS)).status_code == 404


@pytest.mark.asyncio
async def test_token_route(client):
    inst, _ = await _install(client, code="xyz")
    r = await client.get(f"/api/connectors/{inst['id']}/token", headers=HEADERS)
    assert r.status_code == 200
    assert r.json()["access_token"] == "at-xyz"
    # Unknown installation 404s.
    assert (
        await client.get("/api/connectors/ghost/token", headers=HEADERS)
    ).status_code == 404


@pytest.mark.asyncio
async def test_mark_needs_reconnect_route(client):
    inst, _ = await _install(client)
    r = await client.post(
        f"/api/connectors/{inst['id']}/mark-needs-reconnect",
        params={"error_code": "invalid_grant"},
        headers=HEADERS,
    )
    assert r.status_code == 204
    rows = (await client.get("/api/connectors", headers=HEADERS)).json()
    assert rows[0]["needs_reconnect"] is True
    assert rows[0]["last_refresh_error_code"] == "invalid_grant"


# --- agent-scoped enablement ----------------------------------------------


@pytest.mark.asyncio
async def test_agent_enable_disable_and_replace(client):
    inst, _ = await _install(client)
    agent_id = await _new_agent(client)

    # Initially none enabled.
    g = await client.get(f"/api/agents/{agent_id}/connectors", headers=HEADERS)
    assert g.json()["installation_ids"] == []

    # Toggle on.
    t = await client.patch(
        f"/api/agents/{agent_id}/connectors/{inst['id']}",
        json={"enabled": True},
        headers=HEADERS,
    )
    assert t.status_code == 204
    g = await client.get(f"/api/agents/{agent_id}/connectors", headers=HEADERS)
    assert g.json()["installation_ids"] == [inst["id"]]

    # Replace with empty set.
    p = await client.put(
        f"/api/agents/{agent_id}/connectors",
        json={"installation_ids": []},
        headers=HEADERS,
    )
    assert p.status_code == 200
    assert p.json()["installation_ids"] == []


@pytest.mark.asyncio
async def test_agent_enable_unknown_installation(client):
    agent_id = await _new_agent(client)
    t = await client.patch(
        f"/api/agents/{agent_id}/connectors/ghost",
        json={"enabled": True},
        headers=HEADERS,
    )
    assert t.status_code == 404
