"""Rotating the access token (docs/plans/token-rotation.md).

The token is also the key every stored secret is encrypted with, so a
rotation either re-keys all of them and changes what the server accepts, or
it changes nothing. These tests pin both halves — and the refusals, because
the failure this feature exists to prevent is a database nobody can decrypt.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from server.config import settings
from server.crypto import decrypt, encrypt
from server.database import Database
from server.main import app
from server.routers import auth as auth_router
from server.session_manager import session_manager
from server.token_rotation import (
    TokenRotationError,
    env_files_defining_token,
    rotate_auth_token,
    validate_new_token,
)

OLD = "old-token-1234"
NEW = "new-token-abcdef-9876"
HEADERS = {"Authorization": f"Bearer {OLD}"}


@pytest.fixture
def env_file(tmp_path, monkeypatch):
    """An env file that defines the token, found the way the real one is."""
    path = tmp_path / ".env"
    path.write_text(
        "OCTOPUS_HOST=0.0.0.0\nOCTOPUS_AUTH_TOKEN=old-token-1234\nOCTOPUS_PORT=8080\n"
    )
    monkeypatch.setenv("OCTOPUS_ENV_FILE", str(path))
    monkeypatch.setattr(settings, "auth_token", OLD)
    return path


@pytest.fixture
async def db(tmp_path):
    d = Database(str(tmp_path / "rot.db"))
    await d.initialize()
    yield d
    await d.close()


async def _seed_secrets(db: Database) -> None:
    await db._ensure_connected()
    # Parents first: the secret tables are FK-bound to the rows they belong to.
    await db._conn.execute(
        "INSERT INTO backend_credentials "
        "(id, backend, label, auth_type, secret_encrypted, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("cred-1", "claude-code", "key", "api_key", "", "t"),
    )
    await db._conn.execute(
        "INSERT INTO connector_installations "
        "(id, kind, label, auth_type, created_at) VALUES (?, ?, ?, ?, ?)",
        ("inst-1", "github", "acct", "oauth", "t"),
    )
    await db._conn.execute(
        "INSERT INTO credential_secrets (credential_id, secret_encrypted) VALUES (?, ?)",
        ("cred-1", encrypt("sk-ant-secret", OLD)),
    )
    await db._conn.execute(
        "INSERT INTO connector_installation_secrets (installation_id, secret_encrypted) "
        "VALUES (?, ?)",
        ("inst-1", encrypt('{"access_token": "gho_x"}', OLD)),
    )
    await db._conn.execute(
        "INSERT INTO connector_oauth_clients "
        "(kind, client_id, client_secret_encrypted, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("github", "cid", encrypt("client-secret", OLD), "t", "t"),
    )
    await db._conn.commit()


async def _stored(db: Database, table: str, column: str, key_col: str, key: str) -> str:
    cursor = await db._conn.execute(
        f"SELECT {column} FROM {table} WHERE {key_col} = ?", (key,)
    )
    return (await cursor.fetchone())[0]


# ------------------------------------------------------------------ refusals


@pytest.mark.parametrize(
    "candidate, because",
    [
        ("", "empty"),
        ("   ", "blank"),
        ("short", "too short"),
        ("changeme", "the default"),
        ("has spaces in it", "whitespace breaks a URL query"),
        ("semi;colon-token", "a cookie value ends at the semicolon"),
        (OLD, "already the current token"),
    ],
)
def test_a_token_that_would_break_something_is_refused(candidate, because, env_file):
    with pytest.raises(TokenRotationError):
        validate_new_token(candidate)


def test_the_env_file_is_found_by_what_it_defines(env_file):
    assert env_files_defining_token() == [env_file.resolve()]


# ----------------------------------------------------------------- rotating


@pytest.mark.asyncio
async def test_rotation_rekeys_every_secret_and_the_live_token(db, env_file):
    await _seed_secrets(db)

    result = await rotate_auth_token(db, NEW)

    # The live token changed…
    assert settings.auth_token == NEW
    # …the file it has to survive a restart in changed…
    text = env_file.read_text()
    assert f"OCTOPUS_AUTH_TOKEN={NEW}" in text
    # …and nothing else in that file was touched.
    assert "OCTOPUS_HOST=0.0.0.0" in text and "OCTOPUS_PORT=8080" in text

    # …and every secret now opens with the new token, not the old one.
    cred = await _stored(db, "credential_secrets", "secret_encrypted", "credential_id", "cred-1")
    assert decrypt(cred, NEW) == "sk-ant-secret"
    with pytest.raises(ValueError):
        decrypt(cred, OLD)

    inst = await _stored(
        db, "connector_installation_secrets", "secret_encrypted", "installation_id", "inst-1"
    )
    assert decrypt(inst, NEW) == '{"access_token": "gho_x"}'
    client = await _stored(
        db, "connector_oauth_clients", "client_secret_encrypted", "kind", "github"
    )
    assert decrypt(client, NEW) == "client-secret"

    assert result.reencrypted == {
        "credential_secrets": 1,
        "connector_installation_secrets": 1,
        "connector_oauth_clients": 1,
    }


@pytest.mark.asyncio
async def test_a_secret_that_cannot_be_decrypted_changes_nothing(db, env_file):
    """Half a rotation is a database where some secrets are keyed to a token
    nobody has. Better to refuse and say which row is the problem."""
    await _seed_secrets(db)
    await db._conn.execute(
        "INSERT INTO backend_credentials "
        "(id, backend, label, auth_type, secret_encrypted, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("cred-2", "codex", "other", "api_key", "", "t"),
    )
    await db._conn.execute(
        "INSERT INTO credential_secrets (credential_id, secret_encrypted) VALUES (?, ?)",
        ("cred-2", encrypt("keyed-to-something-else", "a-different-token")),
    )
    await db._conn.commit()

    with pytest.raises(TokenRotationError) as e:
        await rotate_auth_token(db, NEW)
    assert "cred-2" in e.value.message

    assert settings.auth_token == OLD
    assert f"OCTOPUS_AUTH_TOKEN={OLD}" in env_file.read_text()
    cred = await _stored(db, "credential_secrets", "secret_encrypted", "credential_id", "cred-1")
    assert decrypt(cred, OLD) == "sk-ant-secret"


@pytest.mark.asyncio
async def test_a_rotation_with_nowhere_to_persist_is_refused(db, tmp_path, monkeypatch):
    """A token that can't survive a restart would leave the next boot unable
    to decrypt the database it just re-keyed."""
    monkeypatch.setenv("OCTOPUS_ENV_FILE", str(tmp_path / "nonexistent.env"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, "auth_token", OLD)

    with pytest.raises(TokenRotationError) as e:
        await rotate_auth_token(db, NEW)
    assert e.value.status_code == 409
    assert settings.auth_token == OLD


@pytest.mark.asyncio
async def test_rotation_works_with_no_secrets_stored(db, env_file):
    result = await rotate_auth_token(db, NEW)
    assert settings.auth_token == NEW
    assert sum(result.reencrypted.values()) == 0


# --------------------------------------------------------------------- route


@pytest.fixture
async def client(db, env_file):
    session_manager.sessions.clear()
    await session_manager.initialize(db)
    auth_router.set_db(db)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_the_route_needs_the_old_token_and_reports_what_it_did(client, db, env_file):
    await _seed_secrets(db)

    assert (
        await client.post("/api/auth/rotate", json={"new_token": NEW})
    ).status_code in (401, 403)

    resp = await client.post(
        "/api/auth/rotate", json={"new_token": NEW}, headers=HEADERS
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["env_files"] == [str(env_file.resolve())]
    assert body["reencrypted"]["credential_secrets"] == 1

    # The old token stops working immediately; the new one works.
    assert (await client.get("/api/sessions", headers=HEADERS)).status_code in (401, 403)
    assert (
        await client.get("/api/sessions", headers={"Authorization": f"Bearer {NEW}"})
    ).status_code == 200


@pytest.mark.asyncio
async def test_open_clients_are_handed_the_new_token_unless_revoking(client, db, env_file):
    seen: list[dict] = []

    async def collect(msg: dict) -> None:
        seen.append(msg)

    session_manager.on_broadcast("rotation-test", collect)
    try:
        await client.post(
            "/api/auth/rotate", json={"new_token": NEW}, headers=HEADERS
        )
        rotated = [m for m in seen if m["type"] == "auth_token_rotated"]
        assert rotated and rotated[-1]["token"] == NEW

        seen.clear()
        third = "third-token-xyz-123"
        await client.post(
            "/api/auth/rotate",
            json={"new_token": third, "revoke_other_clients": True},
            headers={"Authorization": f"Bearer {NEW}"},
        )
        rotated = [m for m in seen if m["type"] == "auth_token_rotated"]
        # Nothing to hand out: every other client must prove it has the new one.
        assert rotated and rotated[-1]["token"] is None
    finally:
        session_manager.remove_broadcast("rotation-test")


@pytest.mark.asyncio
async def test_the_route_refuses_a_weak_token_without_touching_anything(
    client, db, env_file
):
    resp = await client.post(
        "/api/auth/rotate", json={"new_token": "short"}, headers=HEADERS
    )
    assert resp.status_code == 400
    assert settings.auth_token == OLD


@pytest.mark.asyncio
async def test_identity_answers_a_label_and_never_the_token(client, monkeypatch):
    """The sidebar's account row is on screen permanently.

    It used to render `auth_token` as the handle, so the credential was in every
    screenshot and screen share. This is what it reads instead: authenticated,
    so it says nothing to anyone not already holding the token, and answering a
    label that is safe to have on display.
    """
    monkeypatch.setattr(settings, "user_label", "archer")

    assert (await client.get("/api/auth/identity")).status_code == 401

    resp = await client.get("/api/auth/identity", headers=HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"label": "archer"}
    assert settings.auth_token not in resp.text
