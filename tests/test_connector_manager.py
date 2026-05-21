"""ConnectorManager (connectors.md §5.5): install upsert + identity, the
server-side token-refresh / needs_reconnect lifecycle (incl. the per-install
lock that refreshes exactly once under concurrency), and agent enablement."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

import pytest

from server.connector_manager import (
    ConnectorError,
    ConnectorManager,
    _deserialize_token_set,
)
from server.connectors.base import ConnectorBase
from server.connectors.registry import KIND_REGISTRY, register
from server.crypto import decrypt
from server.database import Database
from server.oauth_providers import OAuthTokenSet


class FakeProvider:
    kind = "fakekind"
    authorize_url = "https://fake/auth"
    token_url = "https://fake/token"
    default_scopes = ["s"]
    pkce = True
    client_id = "cid"  # presence of both makes the kind "available"
    client_secret = "csec"

    def __init__(self) -> None:
        self.refresh_calls = 0
        self.refresh_should_fail = False
        self.refresh_delay = 0.0

    def build_authorize_url(self, **k):
        return "https://fake/auth"

    async def exchange_code(self, **k):
        return OAuthTokenSet("at", "rt", time.time() + 3600)

    async def refresh(self, *, client_id, client_secret, refresh_token):
        self.refresh_calls += 1
        if self.refresh_delay:
            await asyncio.sleep(self.refresh_delay)
        if self.refresh_should_fail:
            err = RuntimeError("token revoked")
            err.code = "invalid_grant"  # type: ignore[attr-defined]
            raise err
        return OAuthTokenSet("refreshed", None, time.time() + 3600, scopes=["s"])


class FakeConnector(ConnectorBase):
    kind = "fakekind"
    display_name = "Fake"
    category = "test"
    tools = ("search",)

    def __init__(self, provider: FakeProvider, identity=("ext-1", "me@x.com")):
        self.oauth = provider
        self._identity = identity

    async def fetch_external_identity(self, token_set):
        return self._identity


class UnconfiguredProvider:
    """No client creds → kind shows as unavailable."""

    kind = "noconfig"
    pkce = False


class UnconfiguredConnector(ConnectorBase):
    kind = "noconfig"
    display_name = "NoConfig"
    oauth = UnconfiguredProvider()


@pytest.fixture
def clean_registry():
    saved = dict(KIND_REGISTRY)
    KIND_REGISTRY.clear()
    yield
    KIND_REGISTRY.clear()
    KIND_REGISTRY.update(saved)


@pytest.fixture
async def db():
    d = Database(":memory:")
    await d.initialize()
    yield d
    await d.close()


@pytest.fixture
def provider():
    return FakeProvider()


@pytest.fixture
async def mgr(db, provider, clean_registry):
    register(FakeConnector(provider))
    m = ConnectorManager(db)
    # Configure the kind's OAuth client in-app so it's "available" and the
    # token-refresh path can resolve client creds.
    await m.set_client_creds("fakekind", "cid", "csec")
    return m


def _token_set(expires_in: float = 3600.0) -> OAuthTokenSet:
    return OAuthTokenSet("at", "rt", time.time() + expires_in, scopes=["s"])


async def _agent(db: Database, agent_id: str = "a-1") -> None:
    now = datetime.now(timezone.utc).isoformat()
    await db.save_agent(agent_id=agent_id, name=agent_id, created_at=now, updated_at=now)


# --- catalog ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_catalog_reflects_availability(db, clean_registry, provider):
    register(FakeConnector(provider))
    register(UnconfiguredConnector())
    m = ConnectorManager(db)
    # Nothing configured yet → both unavailable.
    cat = {c["kind"]: c for c in await m.catalog()}
    assert cat["fakekind"]["available"] is False
    assert cat["noconfig"]["available"] is False
    # Setting the OAuth client in-app flips availability.
    await m.set_client_creds("fakekind", "cid", "csec")
    cat = {c["kind"]: c for c in await m.catalog()}
    assert cat["fakekind"]["available"] is True
    assert cat["fakekind"]["display_name"] == "Fake"
    assert cat["noconfig"]["available"] is False


# --- install ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_complete_install_creates_and_encrypts(mgr, db):
    inst = await mgr.complete_install(kind="fakekind", token_set=_token_set())
    assert inst["kind"] == "fakekind"
    assert inst["label"] == "me@x.com"
    assert inst["external_account_id"] == "ext-1"
    assert inst["scopes"] == ["s"]
    # Secret is stored encrypted and round-trips to the original token set.
    blob = await db.get_connector_secret(inst["id"])
    ts = _deserialize_token_set(decrypt(blob, "changeme"))
    assert ts.access_token == "at" and ts.refresh_token == "rt"


@pytest.mark.asyncio
async def test_complete_install_upserts_same_account(mgr, db):
    a = await mgr.complete_install(kind="fakekind", token_set=_token_set())
    b = await mgr.complete_install(kind="fakekind", token_set=_token_set())
    assert a["id"] == b["id"]  # same account → same row, not a duplicate
    assert len(await db.load_connector_installations()) == 1


@pytest.mark.asyncio
async def test_requested_label_overrides_identity(mgr):
    inst = await mgr.complete_install(
        kind="fakekind", token_set=_token_set(), requested_label="Work"
    )
    assert inst["label"] == "Work"


@pytest.mark.asyncio
async def test_unknown_kind_rejected(mgr):
    with pytest.raises(ConnectorError):
        await mgr.complete_install(kind="ghost", token_set=_token_set())


# --- token access / refresh -----------------------------------------------


@pytest.mark.asyncio
async def test_token_no_refresh_when_fresh(mgr, provider):
    inst = await mgr.complete_install(kind="fakekind", token_set=_token_set(3600))
    out = await mgr.get_access_token(inst["id"])
    assert out["access_token"] == "at"
    assert provider.refresh_calls == 0


@pytest.mark.asyncio
async def test_token_refreshes_near_expiry(mgr, db, provider):
    inst = await mgr.complete_install(kind="fakekind", token_set=_token_set(10))
    out = await mgr.get_access_token(inst["id"])
    assert out["access_token"] == "refreshed"
    assert provider.refresh_calls == 1
    # New token persisted; reconnect state stays clean.
    refreshed = await mgr.get_installation(inst["id"])
    assert refreshed["needs_reconnect"] is False
    blob = await db.get_connector_secret(inst["id"])
    ts = _deserialize_token_set(decrypt(blob, "changeme"))
    assert ts.access_token == "refreshed"
    assert ts.refresh_token == "rt"  # carried over when refresh omits it


@pytest.mark.asyncio
async def test_refresh_failure_marks_needs_reconnect(mgr, provider):
    provider.refresh_should_fail = True
    inst = await mgr.complete_install(kind="fakekind", token_set=_token_set(10))
    with pytest.raises(ConnectorError):
        await mgr.get_access_token(inst["id"])
    after = await mgr.get_installation(inst["id"])
    assert after["needs_reconnect"] is True
    assert after["last_refresh_error_code"] == "invalid_grant"


@pytest.mark.asyncio
async def test_concurrent_token_refreshes_once(mgr, provider):
    provider.refresh_delay = 0.05
    inst = await mgr.complete_install(kind="fakekind", token_set=_token_set(10))
    results = await asyncio.gather(
        mgr.get_access_token(inst["id"]), mgr.get_access_token(inst["id"])
    )
    assert provider.refresh_calls == 1  # lock serializes; 2nd sees fresh token
    assert all(r["access_token"] == "refreshed" for r in results)


@pytest.mark.asyncio
async def test_mark_needs_reconnect_and_missing(mgr):
    inst = await mgr.complete_install(kind="fakekind", token_set=_token_set())
    await mgr.mark_needs_reconnect(inst["id"], "invalid_grant")
    assert (await mgr.get_installation(inst["id"]))["needs_reconnect"] is True
    with pytest.raises(ConnectorError):
        await mgr.mark_needs_reconnect("ghost")


@pytest.mark.asyncio
async def test_delete_missing_raises(mgr):
    with pytest.raises(ConnectorError):
        await mgr.delete_installation("ghost")


# --- agent enablement ------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_enable_validates_installation(mgr, db):
    await _agent(db)
    with pytest.raises(ConnectorError):
        await mgr.set_agent_connector("a-1", "ghost", True)
    inst = await mgr.complete_install(kind="fakekind", token_set=_token_set())
    await mgr.set_agent_connector("a-1", inst["id"], True)
    assert await mgr.get_agent_connector_ids("a-1") == [inst["id"]]


@pytest.mark.asyncio
async def test_replace_agent_connectors_diffs(mgr, db):
    await _agent(db)
    i1 = await mgr.complete_install(kind="fakekind", token_set=_token_set())
    # Make a 2nd installation by faking a different account on the connector.
    from server.connectors.registry import get_connector

    get_connector("fakekind")._identity = ("ext-2", "two@x.com")  # type: ignore
    i2 = await mgr.complete_install(kind="fakekind", token_set=_token_set())

    await mgr.set_agent_connector("a-1", i1["id"], True)
    result = await mgr.replace_agent_connectors("a-1", [i2["id"]])
    assert result == [i2["id"]]
    assert await mgr.get_agent_connector_ids("a-1") == [i2["id"]]

    with pytest.raises(ConnectorError):
        await mgr.replace_agent_connectors("a-1", ["ghost"])


@pytest.mark.asyncio
async def test_resolve_client_creds_db_and_env(db, clean_registry, provider, monkeypatch):
    from server.config import settings

    register(FakeConnector(provider))
    m = ConnectorManager(db)

    # Nothing set anywhere.
    assert await m.resolve_client_creds("fakekind") is None

    # Env fallback (github is a real settings field; resolution doesn't require
    # the kind to be registered).
    assert await m.resolve_client_creds("github") is None
    monkeypatch.setattr(settings, "github_oauth_client_id", "env-id")
    monkeypatch.setattr(settings, "github_oauth_client_secret", "env-sec")
    assert await m.resolve_client_creds("github") == ("env-id", "env-sec")

    # DB config for a registered kind; secret round-trips, config hides it.
    await m.set_client_creds("fakekind", "db-id", "db-sec")
    assert await m.resolve_client_creds("fakekind") == ("db-id", "db-sec")
    fcfg = await m.client_config("fakekind", "https://octo.example")
    assert fcfg["configured"] is True
    assert fcfg["client_id"] == "db-id" and fcfg["source"] == "db"
    assert "client_secret" not in fcfg
    assert fcfg["redirect_uri"] == "https://octo.example/api/connectors/oauth/callback"

    # Clearing drops back to "nothing" (fakekind has no env field).
    assert await m.clear_client_creds("fakekind") is True
    assert await m.resolve_client_creds("fakekind") is None

    # Unknown kind can't be configured.
    with pytest.raises(ConnectorError):
        await m.set_client_creds("ghost", "x", "y")


@pytest.mark.asyncio
async def test_enabled_installations_for_agent_views(mgr, db):
    await _agent(db)
    inst = await mgr.complete_install(kind="fakekind", token_set=_token_set())
    await mgr.set_agent_connector("a-1", inst["id"], True)
    views = await mgr.enabled_installations_for_agent("a-1")
    assert len(views) == 1
    assert views[0].id == inst["id"]
    assert views[0].kind == "fakekind"
    assert views[0].label == "me@x.com"
