"""Connector DB layer (connectors.md Phase A): installations split-secret
CRUD + the agent-scoped enable join, including cascade + dedup invariants."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from server.crypto import decrypt, encrypt
from server.database import Database


@pytest.fixture
async def db():
    d = Database(":memory:")
    await d.initialize()
    yield d
    await d.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _make_agent(db: Database, agent_id: str, name: str) -> None:
    now = _now()
    await db.save_agent(agent_id=agent_id, name=name, created_at=now, updated_at=now)


async def _make_installation(
    db: Database,
    installation_id: str = "i-1",
    *,
    kind: str = "github",
    label: str = "octocat",
    external_account_id: str | None = "octocat:1",
    secret: str = "tok-abc",
    scopes: list[str] | None = None,
    enable_by_default: bool = False,
) -> None:
    await db.save_connector_installation(
        installation_id=installation_id,
        kind=kind,
        label=label,
        auth_type="oauth",
        secret_encrypted=encrypt(secret, "auth"),
        created_at=_now(),
        external_account_id=external_account_id,
        scopes=scopes if scopes is not None else ["repo"],
        enable_by_default=enable_by_default,
        token_expires_at=None,
    )


# --- installation CRUD -----------------------------------------------------


@pytest.mark.asyncio
async def test_save_and_load_installation(db):
    await _make_installation(db, "i-1", label="octocat", scopes=["repo", "read:org"])
    rows = await db.load_connector_installations()
    assert len(rows) == 1
    r = rows[0]
    assert r["id"] == "i-1"
    assert r["kind"] == "github"
    assert r["label"] == "octocat"
    assert r["auth_type"] == "oauth"
    assert r["external_account_id"] == "octocat:1"
    assert r["scopes"] == ["repo", "read:org"]
    assert r["enable_by_default"] is False
    assert r["needs_reconnect"] is False
    # The metadata row carries no secret; the blob is split out and decryptable.
    assert "secret_encrypted" not in r
    assert decrypt(await db.get_connector_secret("i-1"), "auth") == "tok-abc"


@pytest.mark.asyncio
async def test_get_installation_none_for_unknown(db):
    assert await db.get_connector_installation("nope") is None
    assert await db.get_connector_secret("nope") is None


@pytest.mark.asyncio
async def test_get_installation_by_account(db):
    await _make_installation(db, "i-1", kind="gmail", external_account_id="me@x.com")
    found = await db.get_connector_installation_by_account("gmail", "me@x.com")
    assert found is not None and found["id"] == "i-1"
    assert await db.get_connector_installation_by_account("gmail", "other@x.com") is None
    assert await db.get_connector_installation_by_account("github", "me@x.com") is None


@pytest.mark.asyncio
async def test_duplicate_account_rejected(db):
    """UNIQUE(kind, external_account_id) blocks a second install of one account."""
    await _make_installation(db, "i-1", kind="gmail", external_account_id="me@x.com")
    with pytest.raises(Exception):
        await _make_installation(db, "i-2", kind="gmail", external_account_id="me@x.com")


@pytest.mark.asyncio
async def test_null_account_not_deduped(db):
    """Mid-install rows (identity unknown) share a NULL account without colliding."""
    await _make_installation(db, "i-1", external_account_id=None)
    await _make_installation(db, "i-2", external_account_id=None)
    assert len(await db.load_connector_installations()) == 2


@pytest.mark.asyncio
async def test_update_installation_metadata_and_secret(db):
    await _make_installation(db, "i-1")
    await db.update_connector_installation(
        "i-1",
        label="renamed",
        external_account_id="octocat:1",
        scopes=["repo", "gist"],
        enable_by_default=True,
        needs_reconnect=True,
        last_refresh_error_code="invalid_grant",
        token_expires_at="2030-01-01T00:00:00+00:00",
        secret_encrypted=encrypt("tok-new", "auth"),
    )
    r = await db.get_connector_installation("i-1")
    assert r["label"] == "renamed"
    assert r["scopes"] == ["repo", "gist"]
    assert r["enable_by_default"] is True
    assert r["needs_reconnect"] is True
    assert r["last_refresh_error_code"] == "invalid_grant"
    assert r["token_expires_at"] == "2030-01-01T00:00:00+00:00"
    assert decrypt(await db.get_connector_secret("i-1"), "auth") == "tok-new"


@pytest.mark.asyncio
async def test_update_can_clear_nullable_and_reconnect(db):
    await _make_installation(db, "i-1")
    await db.update_connector_installation("i-1", needs_reconnect=True,
                                           last_refresh_error_code="invalid_grant")
    # A successful refresh clears the error and the flag.
    await db.update_connector_installation("i-1", needs_reconnect=False,
                                           last_refresh_error_code=None)
    r = await db.get_connector_installation("i-1")
    assert r["needs_reconnect"] is False
    assert r["last_refresh_error_code"] is None


@pytest.mark.asyncio
async def test_delete_installation_cascades_secret(db):
    await _make_installation(db, "i-1")
    assert await db.delete_connector_installation("i-1") is True
    assert await db.get_connector_installation("i-1") is None
    assert await db.get_connector_secret("i-1") is None
    assert await db.delete_connector_installation("i-1") is False


# --- agent-scoped enable join ---------------------------------------------


@pytest.mark.asyncio
async def test_agent_connector_enable_disable(db):
    await _make_agent(db, "a-1", "Researcher")
    await _make_installation(db, "i-1")
    assert await db.get_agent_connector_ids("a-1") == []

    await db.set_agent_connector("a-1", "i-1", True)
    assert await db.get_agent_connector_ids("a-1") == ["i-1"]
    # Idempotent enable.
    await db.set_agent_connector("a-1", "i-1", True)
    assert await db.get_agent_connector_ids("a-1") == ["i-1"]

    await db.set_agent_connector("a-1", "i-1", False)
    assert await db.get_agent_connector_ids("a-1") == []
    # Idempotent disable.
    await db.set_agent_connector("a-1", "i-1", False)
    assert await db.get_agent_connector_ids("a-1") == []


@pytest.mark.asyncio
async def test_enabled_connectors_for_agent_join(db):
    await _make_agent(db, "a-1", "Researcher")
    await _make_installation(db, "i-1", kind="github", label="octocat",
                             external_account_id="octocat:1")
    await _make_installation(db, "i-2", kind="gmail", label="me@x.com",
                             external_account_id="me@x.com")
    await db.set_agent_connector("a-1", "i-1", True)

    enabled = await db.get_enabled_connectors_for_agent("a-1")
    assert [e["id"] for e in enabled] == ["i-1"]
    assert enabled[0]["kind"] == "github"
    assert enabled[0]["label"] == "octocat"


@pytest.mark.asyncio
async def test_join_isolated_per_agent(db):
    await _make_agent(db, "a-1", "A")
    await _make_agent(db, "a-2", "B")
    await _make_installation(db, "i-1")
    await db.set_agent_connector("a-1", "i-1", True)
    assert await db.get_agent_connector_ids("a-1") == ["i-1"]
    assert await db.get_agent_connector_ids("a-2") == []


@pytest.mark.asyncio
async def test_delete_installation_drops_agent_links(db):
    await _make_agent(db, "a-1", "A")
    await _make_installation(db, "i-1")
    await db.set_agent_connector("a-1", "i-1", True)
    await db.delete_connector_installation("i-1")
    assert await db.get_agent_connector_ids("a-1") == []


@pytest.mark.asyncio
async def test_delete_agent_drops_connector_links(db):
    await _make_agent(db, "a-1", "A")
    await _make_installation(db, "i-1")
    await db.set_agent_connector("a-1", "i-1", True)
    await db.delete_agent("a-1")
    # The installation survives; only the link is gone.
    assert await db.get_connector_installation("i-1") is not None
    assert await db.get_enabled_connectors_for_agent("a-1") == []


# --- per-kind OAuth client credentials (in-app config) --------------------


@pytest.mark.asyncio
async def test_oauth_client_crud(db):
    assert await db.get_connector_oauth_client("github") is None
    await db.set_connector_oauth_client("github", "cid", "enc-secret", _now())
    row = await db.get_connector_oauth_client("github")
    assert row["client_id"] == "cid"
    assert row["client_secret_encrypted"] == "enc-secret"

    # Upsert on the kind PK.
    await db.set_connector_oauth_client("github", "cid2", "enc2", _now())
    row = await db.get_connector_oauth_client("github")
    assert row["client_id"] == "cid2" and row["client_secret_encrypted"] == "enc2"

    assert await db.delete_connector_oauth_client("github") is True
    assert await db.get_connector_oauth_client("github") is None
    assert await db.delete_connector_oauth_client("github") is False
