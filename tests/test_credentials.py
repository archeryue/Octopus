"""Tests for the crypto helper + credential DB methods."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from server.crypto import decrypt, encrypt
from server.database import Database


# ---------------------------------------------------------------------------
# crypto
# ---------------------------------------------------------------------------


def test_roundtrip():
    secret = "octopus-token"
    enc = encrypt("sk-ant-12345", secret)
    assert enc != "sk-ant-12345"
    assert decrypt(enc, secret) == "sk-ant-12345"


def test_wrong_key_raises():
    enc = encrypt("plaintext", "key-one")
    with pytest.raises(ValueError):
        decrypt(enc, "key-two")


def test_different_inputs_produce_different_ciphertexts():
    # Fernet bundles a random IV — same input + same key produces different
    # ciphertexts each call. We test by encrypting twice.
    enc_a = encrypt("same", "k")
    enc_b = encrypt("same", "k")
    assert enc_a != enc_b
    assert decrypt(enc_a, "k") == decrypt(enc_b, "k") == "same"


# ---------------------------------------------------------------------------
# database
# ---------------------------------------------------------------------------


@pytest.fixture
async def db():
    d = Database(":memory:")
    await d.initialize()
    yield d
    await d.close()


@pytest.mark.asyncio
async def test_save_and_load_credential(db):
    now = datetime.now(timezone.utc).isoformat()
    await db.save_credential(
        credential_id="c-1",
        backend="claude-code",
        label="Personal",
        auth_type="api_key",
        secret_encrypted=encrypt("sk-secret", "auth"),
        created_at=now,
    )
    rows = await db.load_credentials()
    assert len(rows) == 1
    assert rows[0]["id"] == "c-1"
    assert rows[0]["backend"] == "claude-code"
    assert rows[0]["label"] == "Personal"
    # Decryptable
    assert decrypt(rows[0]["secret_encrypted"], "auth") == "sk-secret"


@pytest.mark.asyncio
async def test_get_credential_returns_none_for_unknown(db):
    assert await db.get_credential("nope") is None


@pytest.mark.asyncio
async def test_update_credential_label_and_secret(db):
    now = datetime.now(timezone.utc).isoformat()
    await db.save_credential(
        "c-2", "claude-code", "Old", "api_key", encrypt("old", "k"), now
    )
    await db.update_credential(
        "c-2", label="New", secret_encrypted=encrypt("new", "k")
    )
    row = await db.get_credential("c-2")
    assert row is not None
    assert row["label"] == "New"
    assert decrypt(row["secret_encrypted"], "k") == "new"


@pytest.mark.asyncio
async def test_update_credential_partial_keeps_other_fields(db):
    """Only `label` changes — secret should stay the same."""
    now = datetime.now(timezone.utc).isoformat()
    await db.save_credential(
        "c-3", "claude-code", "L", "api_key", encrypt("kept", "k"), now
    )
    await db.update_credential("c-3", label="Renamed")
    row = await db.get_credential("c-3")
    assert row["label"] == "Renamed"
    assert decrypt(row["secret_encrypted"], "k") == "kept"


@pytest.mark.asyncio
async def test_delete_credential(db):
    now = datetime.now(timezone.utc).isoformat()
    await db.save_credential(
        "c-4", "codex", "L", "api_key", encrypt("x", "k"), now
    )
    assert await db.delete_credential("c-4") is True
    assert await db.delete_credential("c-4") is False
    assert await db.get_credential("c-4") is None
