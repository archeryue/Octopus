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
