"""Turning this install into its first account (multi-tenancy.md §9).

The one irreversible step in the plan, so what is tested is what a failure
would leave behind — not just the happy path. A half-upgraded install, with an
account whose credentials do not decrypt or with rows nobody owns, is worse
than either the before or the after.
"""

from __future__ import annotations

import json

import pytest
from httpx import ASGITransport, AsyncClient

from server import crypto, deps
from server.bootstrap import BootstrapError, bootstrap_first_account
from server.config import settings
from server.crypto import decrypt, encrypt
from server.database import Database
from server.main import app
from server.routers import auth as auth_router
from server.session_manager import session_manager
from server.users import UserManager
from server.workspace import paths_for


async def _stored_secret(db, credential_id: str) -> str:
    """The ciphertext as it sits in the split-out secrets table."""
    cursor = await db.conn.execute(
        "SELECT secret_encrypted FROM credential_secrets WHERE credential_id = ?",
        (credential_id,),
    )
    row = await cursor.fetchone()
    return row[0]


@pytest.fixture
async def legacy(tmp_path, monkeypatch):
    """An install as it was before accounts: rows with no owner, secrets keyed
    to `OCTOPUS_AUTH_TOKEN`, sessions working outside any workspace."""
    monkeypatch.setattr(settings, "master_key", "test-master-key")
    monkeypatch.setattr(settings, "auth_token", "the-old-token")
    monkeypatch.setattr(settings, "users_root", str(tmp_path / "users"))
    monkeypatch.setattr(settings, "agents_dir", str(tmp_path / "legacy-agents"))
    crypto._MASTER_CACHE.clear()

    db = Database(":memory:")
    await db.initialize()
    session_manager.sessions.clear()
    await session_manager.initialize(db)

    agent = await db.get_system_agent()
    await db.save_session(
        session_id="s-old",
        name="work in progress",
        working_dir="/home/somebody/a-repo",
        created_at="2026-01-01T00:00:00Z",
        agent_id=agent["id"],
    )
    await db.save_credential(
        credential_id="cred-1",
        backend="claude-code",
        label="Claude",
        auth_type="api_key",
        secret_encrypted=encrypt("sk-ant-the-real-key", "the-old-token"),
        created_at="2026-01-01T00:00:00Z",
    )
    # Agent memory where a pre-accounts install keeps it.
    legacy_memory = tmp_path / "legacy-agents" / agent["id"] / "memory"
    legacy_memory.mkdir(parents=True)
    (legacy_memory / "MEMORY.md").write_text("- [Magic word](magic.md)\n")

    try:
        yield db, agent
    finally:
        await db.close()
        session_manager.sessions.clear()


@pytest.mark.asyncio
async def test_the_upgrade_hands_over_everything_that_was_here(legacy):
    db, agent = legacy

    summary = await bootstrap_first_account(
        db, username="archer", password="password1"
    )

    user = await db.get_user(summary["user_id"])
    assert user["is_admin"] is True, "the first account has to be able to invite"

    # The credential still opens — with the account's key, not the old token.
    row = await _stored_secret(db, "cred-1")
    users = UserManager(db)
    assert decrypt(row, users.data_key(user)) == "sk-ant-the-real-key"
    with pytest.raises(ValueError):
        decrypt(row, "the-old-token")

    # Every row belongs to them, and nothing is left unowned.
    assert summary["adopted"].get("sessions") == 1
    assert await db.count_orphan_rows() == {}

    # Their existing working directory keeps working, which is the difference
    # between an upgrade and a breakage.
    assert "/home/somebody/a-repo" in json.loads(user["extra_roots"])

    # Agent memory moved under them rather than being left behind.
    moved = paths_for(user["id"]).agents / agent["id"] / "memory" / "MEMORY.md"
    assert moved.exists() and "Magic word" in moved.read_text()


@pytest.mark.asyncio
async def test_a_secret_that_will_not_decrypt_changes_nothing(legacy):
    """The failure that matters. Re-keying reads everything before it writes
    anything, so a row that cannot be decrypted costs nothing rather than
    leaving half the secrets keyed to something nobody has."""
    db, _agent = legacy
    await db.save_credential(
        credential_id="cred-broken",
        backend="claude-code",
        label="Corrupt",
        auth_type="api_key",
        secret_encrypted="not-a-ciphertext",
        created_at="2026-01-01T00:00:00Z",
    )

    with pytest.raises(BootstrapError):
        await bootstrap_first_account(db, username="archer", password="password1")

    # The good secret is untouched and still opens with the old token.
    assert decrypt(await _stored_secret(db, "cred-1"), "the-old-token")


@pytest.mark.asyncio
async def test_it_can_only_happen_once(legacy):
    db, _agent = legacy
    await bootstrap_first_account(db, username="archer", password="password1")
    with pytest.raises(BootstrapError) as e:
        await bootstrap_first_account(db, username="vera", password="password2")
    assert e.value.status_code == 409


@pytest.mark.asyncio
async def test_the_route_is_the_only_way_in_and_closes_behind_itself(legacy):
    """The install token authenticates the upgrade and stops working the
    instant it succeeds — the same fact stated twice."""
    db, _agent = legacy
    auth_router.set_db(db)
    deps.set_user_manager(UserManager(db))

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        state = await c.get("/api/auth/state")
        assert state.status_code == 200 and state.json()["accounts_exist"] is False

        res = await c.post(
            "/api/auth/bootstrap",
            json={"username": "archer", "password": "password1"},
            headers={"Authorization": "Bearer the-old-token"},
        )
        assert res.status_code == 200
        token = res.json()["token"]

        # The install token is now dead, the session bearer works, and the
        # screen knows which question to ask.
        assert (
            await c.get(
                "/api/auth/identity", headers={"Authorization": "Bearer the-old-token"}
            )
        ).status_code == 401
        assert (
            await c.get(
                "/api/auth/identity", headers={"Authorization": f"Bearer {token}"}
            )
        ).json()["label"] == "archer"
        assert (await c.get("/api/auth/state")).json()["accounts_exist"] is True

        # And it cannot be used a second time, even with the old token.
        again = await c.post(
            "/api/auth/bootstrap",
            json={"username": "vera", "password": "password2"},
            headers={"Authorization": "Bearer the-old-token"},
        )
        assert again.status_code == 401
