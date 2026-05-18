"""Tests for the file/image attachment feature.

Covers the on-disk storage module, the upload/download HTTP endpoints,
and the session_manager glue that resolves attachment ids → absolute
file paths and prepends an `<attachments>` header to the prompt.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from server import attachments as att
from server.attachments import (
    MAX_FILE_BYTES,
    AttachmentError,
    delete_session_attachments,
    get_path,
    save_upload,
)
from server.database import Database
from server.main import app
from server.session_manager import session_manager

TOKEN = "changeme"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def attachments_root(tmp_path, monkeypatch):
    """Redirect attachments storage to a per-test tmpdir.

    The module reads `settings.attachments_dir` at call time, so a plain
    monkeypatch.setattr on the setting is enough — no module re-import.
    """
    from server.config import settings

    monkeypatch.setattr(settings, "attachments_dir", str(tmp_path))
    return tmp_path


@pytest.fixture
async def client(attachments_root):
    """In-memory DB + ASGI client, same shape as test_api.client."""
    db = Database(":memory:")
    await db.initialize()
    session_manager.sessions.clear()
    await session_manager.initialize(db)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c

    await db.close()


# ---------------------------------------------------------------------------
# Storage module — unit-level
# ---------------------------------------------------------------------------


def test_save_upload_writes_file_and_returns_metadata(attachments_root):
    rec = save_upload("sess-abc", "hello.txt", b"hi there", declared_mime="text/plain")
    assert rec.size == 8
    assert rec.filename == "hello.txt"
    assert rec.mime_type == "text/plain"
    assert rec.path.is_file()
    assert rec.path.read_bytes() == b"hi there"
    # Layout is `<id>__<filename>` inside the session subdir
    assert rec.path.parent.name == "sess-abc"
    assert rec.path.name.endswith("__hello.txt")
    assert rec.path.name.startswith(rec.id + "__")


def test_save_upload_rejects_oversize(attachments_root):
    big = b"x" * (MAX_FILE_BYTES + 1)
    with pytest.raises(AttachmentError):
        save_upload("s", "big.bin", big)


def test_save_upload_rejects_empty(attachments_root):
    with pytest.raises(AttachmentError):
        save_upload("s", "empty.txt", b"")


def test_save_upload_sanitizes_filename(attachments_root):
    rec = save_upload("s", "../../../etc/passwd", b"data")
    # Path separators stripped; .. runs collapsed; file lands inside session dir
    assert "/" not in rec.path.name
    assert "\\" not in rec.path.name
    assert rec.path.parent.name == "s"
    assert ".." not in rec.filename


def test_save_upload_handles_blank_filename(attachments_root):
    rec = save_upload("s", "", b"data")
    assert rec.filename  # always non-empty fallback
    assert rec.path.is_file()


def test_get_path_roundtrip(attachments_root):
    rec = save_upload("s", "hello.txt", b"data")
    found = get_path("s", rec.id)
    assert found == rec.path


def test_get_path_missing_returns_none(attachments_root):
    assert get_path("nonexistent", "abcdef123456") is None
    rec = save_upload("s", "hello.txt", b"data")
    assert get_path("s", "wrongid000000") is None
    # Real id still resolves
    assert get_path("s", rec.id) == rec.path


def test_get_path_rejects_traversal(attachments_root):
    save_upload("s", "ok.txt", b"data")
    # Path separators in the id can't escape the session dir
    assert get_path("s", "../other") is None
    assert get_path("s", "..\\other") is None


def test_delete_session_attachments_wipes_dir(attachments_root):
    save_upload("s", "a.txt", b"1")
    save_upload("s", "b.txt", b"2")
    session_dir = attachments_root / "s"
    assert session_dir.is_dir()
    delete_session_attachments("s")
    assert not session_dir.exists()
    # Idempotent
    delete_session_attachments("s")


# ---------------------------------------------------------------------------
# HTTP endpoints
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upload_and_download_roundtrip(client):
    create = await client.post(
        "/api/sessions", headers=HEADERS, json={"name": "U"}
    )
    sid = create.json()["id"]

    files = {"file": ("hello.txt", b"world", "text/plain")}
    up = await client.post(
        f"/api/sessions/{sid}/attachments", headers=HEADERS, files=files
    )
    assert up.status_code == 201
    meta = up.json()
    assert meta["filename"] == "hello.txt"
    assert meta["size"] == 5
    assert meta["mime_type"] == "text/plain"
    aid = meta["id"]

    # Bearer-header download
    dn = await client.get(
        f"/api/sessions/{sid}/attachments/{aid}", headers=HEADERS
    )
    assert dn.status_code == 200
    assert dn.content == b"world"

    # Query-token download (the path <img src> takes)
    dn_q = await client.get(
        f"/api/sessions/{sid}/attachments/{aid}?token={TOKEN}"
    )
    assert dn_q.status_code == 200
    assert dn_q.content == b"world"


@pytest.mark.asyncio
async def test_upload_requires_auth(client):
    create = await client.post(
        "/api/sessions", headers=HEADERS, json={"name": "Auth"}
    )
    sid = create.json()["id"]
    files = {"file": ("x.txt", b"x", "text/plain")}
    resp = await client.post(f"/api/sessions/{sid}/attachments", files=files)
    assert resp.status_code in (401, 403)


@pytest.mark.asyncio
async def test_upload_unknown_session_404(client):
    files = {"file": ("x.txt", b"x", "text/plain")}
    resp = await client.post(
        "/api/sessions/nope/attachments", headers=HEADERS, files=files
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_download_missing_404(client):
    create = await client.post(
        "/api/sessions", headers=HEADERS, json={"name": "M"}
    )
    sid = create.json()["id"]
    resp = await client.get(
        f"/api/sessions/{sid}/attachments/doesnotexist", headers=HEADERS
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_download_wrong_token_401(client):
    create = await client.post(
        "/api/sessions", headers=HEADERS, json={"name": "WT"}
    )
    sid = create.json()["id"]
    files = {"file": ("x.txt", b"x", "text/plain")}
    up = await client.post(
        f"/api/sessions/{sid}/attachments", headers=HEADERS, files=files
    )
    aid = up.json()["id"]
    # Wrong query token
    resp = await client.get(
        f"/api/sessions/{sid}/attachments/{aid}?token=wrong"
    )
    assert resp.status_code == 401
    # No token at all
    resp = await client.get(f"/api/sessions/{sid}/attachments/{aid}")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_delete_session_cleans_up_attachments(client, attachments_root):
    create = await client.post(
        "/api/sessions", headers=HEADERS, json={"name": "Cleanup"}
    )
    sid = create.json()["id"]
    files = {"file": ("a.txt", b"hi", "text/plain")}
    await client.post(
        f"/api/sessions/{sid}/attachments", headers=HEADERS, files=files
    )
    session_dir = Path(attachments_root) / sid
    assert session_dir.is_dir()

    await client.delete(f"/api/sessions/{sid}", headers=HEADERS)
    assert not session_dir.exists()


# ---------------------------------------------------------------------------
# session_manager glue — attachment_ids prepend a header to the prompt
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_message_prepends_attachment_header(
    client, attachments_root, monkeypatch
):
    """An uploaded attachment's absolute path is injected into the prompt
    that hits the backend, so the agent can `Read` it.

    We stub `_run_backend` to capture the prompt rather than spawning a
    real CLI — that's a separate integration concern.
    """
    create = await client.post(
        "/api/sessions", headers=HEADERS, json={"name": "P"}
    )
    sid = create.json()["id"]
    files = {"file": ("notes.txt", b"hello", "text/plain")}
    up = await client.post(
        f"/api/sessions/{sid}/attachments", headers=HEADERS, files=files
    )
    aid = up.json()["id"]

    captured: dict = {}

    async def fake_run(session, prompt):
        captured["prompt"] = prompt
        captured["session_id"] = session.id
        if False:
            yield {}  # turn into async generator

    monkeypatch.setattr(session_manager, "_run_backend", fake_run)

    # Drive a single turn through send_message (the WS path does the same).
    async for _evt in session_manager.send_message(
        sid, "what's in this file?", attachment_ids=[aid]
    ):
        pass

    prompt = captured["prompt"]
    assert "<attachments>" in prompt
    assert "</attachments>" in prompt
    # Absolute path to the file we uploaded should be in the header
    expected_path = get_path(sid, aid)
    assert expected_path is not None
    assert str(expected_path) in prompt
    # Original user text still present, after the header
    assert "what's in this file?" in prompt
    assert prompt.index("</attachments>") < prompt.index("what's in this file?")


@pytest.mark.asyncio
async def test_send_message_no_attachments_unchanged(client, monkeypatch):
    """No attachments → no `<attachments>` block, prompt is verbatim."""
    create = await client.post(
        "/api/sessions", headers=HEADERS, json={"name": "Plain"}
    )
    sid = create.json()["id"]

    captured: dict = {}

    async def fake_run(session, prompt):
        captured["prompt"] = prompt
        if False:
            yield {}

    monkeypatch.setattr(session_manager, "_run_backend", fake_run)

    async for _evt in session_manager.send_message(sid, "hello there"):
        pass

    assert captured["prompt"] == "hello there"


@pytest.mark.asyncio
async def test_send_message_missing_attachment_id_is_dropped(
    client, attachments_root, monkeypatch
):
    """An orphaned id (deleted file, wrong id) is silently skipped so the
    user's text still goes through."""
    create = await client.post(
        "/api/sessions", headers=HEADERS, json={"name": "Drop"}
    )
    sid = create.json()["id"]

    captured: dict = {}

    async def fake_run(session, prompt):
        captured["prompt"] = prompt
        if False:
            yield {}

    monkeypatch.setattr(session_manager, "_run_backend", fake_run)

    async for _evt in session_manager.send_message(
        sid, "still send", attachment_ids=["nonexistent"]
    ):
        pass

    # No header (no real attachments resolved), prompt unchanged
    assert captured["prompt"] == "still send"


@pytest.mark.asyncio
async def test_user_message_persists_attachments_metadata(
    client, attachments_root, monkeypatch
):
    """After a turn with attachments, the DB row for the user message
    carries the metadata so the chat history can re-render the chips on
    reload."""
    create = await client.post(
        "/api/sessions", headers=HEADERS, json={"name": "Persist"}
    )
    sid = create.json()["id"]
    files = {"file": ("doc.md", b"x", "text/markdown")}
    up = await client.post(
        f"/api/sessions/{sid}/attachments", headers=HEADERS, files=files
    )
    aid = up.json()["id"]

    async def fake_run(session, prompt):
        if False:
            yield {}

    monkeypatch.setattr(session_manager, "_run_backend", fake_run)
    async for _evt in session_manager.send_message(
        sid, "look", attachment_ids=[aid]
    ):
        pass

    msgs = await session_manager.db.load_messages(sid)
    user_msgs = [m for m in msgs if m["role"] == "user"]
    assert user_msgs, "user message wasn't persisted"
    assert user_msgs[0]["attachments"]
    persisted = user_msgs[0]["attachments"][0]
    assert persisted["id"] == aid
    assert persisted["filename"] == "doc.md"


@pytest.mark.asyncio
async def test_start_message_caps_attachment_count(client):
    """Too many attachments → ValueError surfacing as a WS error."""
    create = await client.post(
        "/api/sessions", headers=HEADERS, json={"name": "Cap"}
    )
    sid = create.json()["id"]
    too_many = [f"id{i}" for i in range(50)]
    with pytest.raises(ValueError, match="too many"):
        await session_manager.start_message(sid, "x", attachment_ids=too_many)


# ---------------------------------------------------------------------------
# DB migration — pre-existing DB without `attachments` column still works
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_attachments_column_migration(tmp_path):
    """An old DB created before the attachments column existed gets the
    column added on next initialize and reads back []."""
    db_path = tmp_path / "legacy.db"
    import aiosqlite

    # Create the legacy messages table (no `attachments` column).
    conn = await aiosqlite.connect(str(db_path))
    await conn.execute(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, name TEXT, "
        "working_dir TEXT, created_at TEXT, claude_session_id TEXT)"
    )
    await conn.execute(
        "CREATE TABLE messages ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "session_id TEXT NOT NULL, "
        "seq INTEGER NOT NULL, "
        "role TEXT NOT NULL, "
        "type TEXT NOT NULL, "
        "content TEXT, "
        "tool_name TEXT, "
        "tool_input TEXT, "
        "tool_use_id TEXT, "
        "is_error INTEGER, "
        "session_id_ref TEXT, "
        "cost REAL"
        ")"
    )
    await conn.execute(
        "INSERT INTO sessions (id, name, working_dir, created_at) "
        "VALUES ('legacy', 'L', '/tmp', '2024-01-01T00:00:00Z')"
    )
    await conn.execute(
        "INSERT INTO messages (session_id, seq, role, type, content) "
        "VALUES ('legacy', 0, 'user', 'text', '\"hi\"')"
    )
    await conn.commit()
    await conn.close()

    # Open via the real Database — migration should add the column.
    db = Database(str(db_path))
    await db.initialize()
    msgs = await db.load_messages("legacy")
    assert len(msgs) == 1
    assert msgs[0]["attachments"] == []  # default for legacy rows
    await db.close()
