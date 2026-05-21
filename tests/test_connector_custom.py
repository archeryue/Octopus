"""Custom (user-defined) connectors (connectors.md custom-connectors): the
generic OAuth provider, CustomConnector, resolve_connector merge, and the
manager's create/delete."""

from __future__ import annotations

import datetime as dt
import time

import pytest

from server.connector_manager import ConnectorError, ConnectorManager
from server.connectors.base import ConnectorInstallation
from server.connectors.custom import (
    CustomConnector,
    GenericOAuthProvider,
    resolve_connector,
)
from server.database import Database
from server.oauth_providers import OAuthTokenSet


@pytest.fixture
async def db():
    d = Database(":memory:")
    await d.initialize()
    yield d
    await d.close()


def _row(**over):
    base = dict(
        kind="linear",
        display_name="Linear",
        authorize_url="https://linear.app/oauth/authorize",
        token_url="https://api.linear.app/oauth/token",
        scopes=["read", "write"],
        pkce=True,
        api_base="https://api.linear.app/graphql",
    )
    base.update(over)
    return base


def _fake_client(body: dict, status: int = 200):
    class FakeResp:
        status_code = status
        content = b"x"

        def json(self):
            return body

        def raise_for_status(self):
            if status >= 400:
                raise RuntimeError(f"HTTP {status}")

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, data=None, headers=None):
            return FakeResp()

    return FakeClient


# --- generic OAuth provider ------------------------------------------------


def test_generic_authorize_url_pkce():
    p = GenericOAuthProvider(
        kind="x", authorize_url="https://x/auth", token_url="t",
        default_scopes=["a", "b"], pkce=True,
    )
    url = p.build_authorize_url(
        client_id="cid", redirect_uri="https://app/cb", code_challenge="chal", state="s"
    )
    assert "client_id=cid" in url and "response_type=code" in url
    assert "scope=a+b" in url
    assert "code_challenge=chal" in url and "code_challenge_method=S256" in url


def test_generic_authorize_url_no_pkce_and_existing_query():
    p = GenericOAuthProvider(
        kind="x", authorize_url="https://x/auth?foo=1", token_url="t",
        default_scopes=[], pkce=False,
    )
    url = p.build_authorize_url(
        client_id="cid", redirect_uri="https://app/cb", code_challenge="chal", state="s"
    )
    assert "code_challenge" not in url
    assert "&client_id=cid" in url  # appended with & since URL already has ?


@pytest.mark.asyncio
async def test_generic_exchange_code_comma_scopes(monkeypatch):
    import server.connectors.custom as cu

    monkeypatch.setattr(
        cu.httpx,
        "AsyncClient",
        _fake_client(
            {"access_token": "at", "refresh_token": "rt", "expires_in": 3600,
             "scope": "read,write"}
        ),
    )
    p = GenericOAuthProvider(kind="x", authorize_url="a", token_url="t",
                             default_scopes=[], pkce=True)
    ts = await p.exchange_code(
        client_id="cid", client_secret="csec", code="c", redirect_uri="r",
        code_verifier="v", state="s",
    )
    assert ts.access_token == "at" and ts.refresh_token == "rt"
    assert ts.scopes == ["read", "write"]  # comma split
    assert ts.expires_at_epoch > time.time()


@pytest.mark.asyncio
async def test_generic_refresh_keeps_old_token(monkeypatch):
    import server.connectors.custom as cu

    monkeypatch.setattr(
        cu.httpx, "AsyncClient", _fake_client({"access_token": "at2", "expires_in": 3600})
    )
    p = GenericOAuthProvider(kind="x", authorize_url="a", token_url="t",
                             default_scopes=[], pkce=True)
    ts = await p.refresh(client_id="cid", client_secret="csec", refresh_token="old")
    assert ts.access_token == "at2" and ts.refresh_token == "old"


# --- CustomConnector -------------------------------------------------------


def test_custom_connector_shape():
    c = CustomConnector(_row())
    assert c.kind == "linear" and c.display_name == "Linear"
    assert c.is_custom is True and c.tools == ("request",)
    assert c.mcp_module == "server.mcp_servers.connectors.custom"
    inst = ConnectorInstallation(id="abcdef123456", kind="linear", label="Linear")
    entry = c.mcp_entry(inst, {"OCTOPUS_API_BASE": "x", "PYTHONPATH": "p"})
    assert entry["args"] == ["-m", "server.mcp_servers.connectors.custom"]
    assert entry["env"]["OCTOPUS_INSTALLATION_ID"] == "abcdef123456"
    assert entry["env"]["OCTOPUS_CONNECTOR_API_BASE"] == "https://api.linear.app/graphql"
    assert c.mcp_key(inst) == "linear_abcdef"


@pytest.mark.asyncio
async def test_custom_connector_identity_is_display_name():
    ext, label = await CustomConnector(_row()).fetch_external_identity(
        OAuthTokenSet("at", None, 0.0)
    )
    assert ext == "" and label == "Linear"


# --- resolve_connector (built-in vs custom vs none) -----------------------


@pytest.mark.asyncio
async def test_resolve_connector(db):
    assert (await resolve_connector(db, "github")).kind == "github"
    assert await resolve_connector(db, "ghost") is None
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    await db.save_custom_connector(
        kind="linear", display_name="Linear", authorize_url="a", token_url="t",
        scopes=["x"], pkce=True, api_base="https://api.linear.app", now=now,
    )
    c = await resolve_connector(db, "linear")
    assert isinstance(c, CustomConnector) and c.kind == "linear"


# --- manager create / delete ----------------------------------------------


@pytest.mark.asyncio
async def test_manager_create_and_delete_custom(db):
    m = ConnectorManager(db)
    await m.create_custom_connector(
        kind="Linear",  # mixed case → lowercased
        display_name="Linear",
        authorize_url="https://l/auth",
        token_url="https://l/tok",
        scopes=["read"],
        pkce=True,
        api_base="https://api.linear.app",
        client_id="cid",
        client_secret="csec",
    )
    cat = {c["kind"]: c for c in await m.catalog()}
    assert cat["linear"]["custom"] is True
    assert cat["linear"]["available"] is True  # creds stored during create
    assert await m.resolve_client_creds("linear") == ("cid", "csec")

    # Duplicate, built-in collision, and bad slug all rejected.
    for kind in ("linear", "github", "Bad Slug!"):
        with pytest.raises(ConnectorError):
            await m.create_custom_connector(
                kind=kind, display_name="x", authorize_url="a", token_url="t",
                scopes=[], pkce=False, api_base="b", client_id="i", client_secret="s",
            )

    # Delete tears down definition + creds.
    await m.delete_custom_connector("linear")
    assert await resolve_connector(db, "linear") is None
    assert await m.resolve_client_creds("linear") is None
    with pytest.raises(ConnectorError):
        await m.delete_custom_connector("linear")


@pytest.mark.asyncio
async def test_delete_custom_cascades_installations(db):
    m = ConnectorManager(db)
    await m.create_custom_connector(
        kind="linear", display_name="Linear", authorize_url="a", token_url="t",
        scopes=[], pkce=False, api_base="https://api.linear.app",
        client_id="cid", client_secret="csec",
    )
    inst = await m.complete_install(
        kind="linear", token_set=OAuthTokenSet("at", "rt", time.time() + 3600)
    )
    assert await m.get_installation(inst["id"]) is not None
    await m.delete_custom_connector("linear")
    # Installation of that kind is gone too.
    assert await m.get_installation(inst["id"]) is None
