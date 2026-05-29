"""Tests for the in-app file viewer.

Covers:
  * `server.file_viewer.resolve_safe_path` — the single security gate
    every viewer entry point funnels through. The most important tests
    are the negative ones (escape, symlink escape, missing, oversize).
  * `GET /api/sessions/{id}/files{,/meta}` — wired endpoint, including
    auth (header + query-token both accepted) and proper status codes
    for each FileViewerError subclass.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from server.database import Database
from server.file_viewer import (
    MAX_FILE_BYTES,
    FileNotFound,
    FileTooLarge,
    PathRejected,
    UnsupportedType,
    resolve_safe_path,
)
from server.main import app
from server.session_manager import session_manager

TOKEN = "changeme"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


# ---------------------------------------------------------------------------
# resolve_safe_path — security unit tests
# ---------------------------------------------------------------------------


def test_resolves_simple_relative_path(tmp_path):
    (tmp_path / "doc.md").write_text("# hi")
    r = resolve_safe_path(tmp_path, "doc.md")
    assert r.relative_path == "doc.md"
    assert r.kind == "markdown"
    assert r.mime_type.startswith("text/markdown")
    assert r.size == 4


def test_resolves_nested_relative_path(tmp_path):
    sub = tmp_path / "sub" / "deep"
    sub.mkdir(parents=True)
    (sub / "file.py").write_text("x = 1\n")
    r = resolve_safe_path(tmp_path, "sub/deep/file.py")
    assert r.kind == "code"
    assert r.relative_path == str(Path("sub/deep/file.py"))


def test_accepts_absolute_path_inside_root(tmp_path):
    (tmp_path / "a.txt").write_text("hello")
    r = resolve_safe_path(tmp_path, str(tmp_path / "a.txt"))
    assert r.relative_path == "a.txt"


def test_rejects_empty_path(tmp_path):
    with pytest.raises(PathRejected):
        resolve_safe_path(tmp_path, "")
    with pytest.raises(PathRejected):
        resolve_safe_path(tmp_path, "   ")


def test_rejects_tilde_path(tmp_path):
    with pytest.raises(PathRejected):
        resolve_safe_path(tmp_path, "~/secret")


def test_rejects_dotdot_escape(tmp_path):
    sub = tmp_path / "inside"
    sub.mkdir()
    (tmp_path.parent / "outside.txt").write_text("nope")
    with pytest.raises(PathRejected):
        resolve_safe_path(sub, "../outside.txt")


def test_rejects_absolute_path_outside_root(tmp_path):
    (tmp_path.parent / "elsewhere.txt").write_text("nope")
    with pytest.raises(PathRejected):
        resolve_safe_path(tmp_path, str(tmp_path.parent / "elsewhere.txt"))


def test_rejects_symlink_escaping_root(tmp_path):
    outside_root = tmp_path / "outside"
    outside_root.mkdir()
    (outside_root / "secret.md").write_text("# secret")
    inside_root = tmp_path / "inside"
    inside_root.mkdir()
    # A symlink in /inside that points to /outside/secret.md.
    # realpath resolves it; relative_to(/inside) must fail.
    (inside_root / "link.md").symlink_to(outside_root / "secret.md")
    with pytest.raises(PathRejected):
        resolve_safe_path(inside_root, "link.md")


def test_missing_file_raises_filenotfound(tmp_path):
    with pytest.raises(FileNotFound):
        resolve_safe_path(tmp_path, "nope.md")


def test_directory_path_raises_filenotfound(tmp_path):
    (tmp_path / "subdir").mkdir()
    with pytest.raises(FileNotFound):
        resolve_safe_path(tmp_path, "subdir")


def test_oversized_file_raises_filetoolarge(tmp_path):
    big = tmp_path / "huge.md"
    big.write_bytes(b"x" * (MAX_FILE_BYTES + 1))
    with pytest.raises(FileTooLarge):
        resolve_safe_path(tmp_path, "huge.md")


def test_unsupported_extension_raises(tmp_path):
    (tmp_path / "blob.bin").write_bytes(b"\x00\x01\x02")
    with pytest.raises(UnsupportedType):
        resolve_safe_path(tmp_path, "blob.bin")


def test_extensionless_dockerfile_allowed(tmp_path):
    (tmp_path / "Dockerfile").write_text("FROM python\n")
    r = resolve_safe_path(tmp_path, "Dockerfile")
    assert r.kind == "code"


def test_extensionless_readme_allowed(tmp_path):
    (tmp_path / "README").write_text("hi\n")
    r = resolve_safe_path(tmp_path, "README")
    assert r.kind == "code"


def test_classify_image(tmp_path):
    (tmp_path / "shot.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
    r = resolve_safe_path(tmp_path, "shot.png")
    assert r.kind == "image"
    assert r.mime_type == "image/png"


def test_classify_pdf(tmp_path):
    (tmp_path / "doc.pdf").write_bytes(b"%PDF-1.4\n%fake")
    r = resolve_safe_path(tmp_path, "doc.pdf")
    assert r.kind == "pdf"
    assert r.mime_type == "application/pdf"


def test_classify_text_log(tmp_path):
    (tmp_path / "out.log").write_text("hello\n")
    r = resolve_safe_path(tmp_path, "out.log")
    assert r.kind == "text"
    assert r.mime_type == "text/plain; charset=utf-8"


def test_classify_yaml_is_code(tmp_path):
    (tmp_path / "ci.yaml").write_text("on: push\n")
    r = resolve_safe_path(tmp_path, "ci.yaml")
    assert r.kind == "code"


# ---------------------------------------------------------------------------
# /api/sessions/{id}/files endpoint
# ---------------------------------------------------------------------------


@pytest.fixture
async def client(tmp_path):
    """In-memory DB + ASGI client, mirrors test_attachments.client."""
    db = Database(":memory:")
    await db.initialize()
    session_manager.sessions.clear()
    await session_manager.initialize(db)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    await db.close()


@pytest.fixture
async def session_with_files(client, tmp_path):
    """Create a live session whose working_dir is a fresh tmpdir, and
    populate it with files of various kinds. Returns (session_id, root)."""
    root = tmp_path / "wd"
    root.mkdir()
    (root / "plan.md").write_text("# plan\nstep one")
    (root / "main.py").write_text("def main():\n    pass\n")
    (root / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    (root / "doc.pdf").write_bytes(b"%PDF-1.4\nfake")
    (root / "blob.bin").write_bytes(b"\x00\x01")
    (root / "huge.md").write_bytes(b"x" * (MAX_FILE_BYTES + 1))
    sub = root / "sub"
    sub.mkdir()
    (sub / "note.txt").write_text("nested")
    (root.parent / "outside.md").write_text("escape me")

    agent = await session_manager.db.get_system_agent()
    sess = await session_manager.create_session(
        agent["id"], name="viewer-test", working_dir=str(root)
    )
    return sess.id, root


async def test_get_file_markdown(client, session_with_files):
    sid, _ = session_with_files
    r = await client.get(
        f"/api/sessions/{sid}/files",
        params={"path": "plan.md"},
        headers=HEADERS,
    )
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/markdown")
    assert b"# plan" in r.content


async def test_get_file_code(client, session_with_files):
    sid, _ = session_with_files
    r = await client.get(
        f"/api/sessions/{sid}/files",
        params={"path": "main.py"},
        headers=HEADERS,
    )
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    assert b"def main" in r.content


async def test_get_file_image(client, session_with_files):
    sid, _ = session_with_files
    r = await client.get(
        f"/api/sessions/{sid}/files",
        params={"path": "logo.png"},
        headers=HEADERS,
    )
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"


async def test_get_file_nested_path(client, session_with_files):
    sid, _ = session_with_files
    r = await client.get(
        f"/api/sessions/{sid}/files",
        params={"path": "sub/note.txt"},
        headers=HEADERS,
    )
    assert r.status_code == 200
    assert r.content == b"nested"


async def test_get_file_meta_returns_kind(client, session_with_files):
    sid, _ = session_with_files
    r = await client.get(
        f"/api/sessions/{sid}/files/meta",
        params={"path": "plan.md"},
        headers=HEADERS,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["kind"] == "markdown"
    assert body["mime_type"].startswith("text/markdown")
    assert body["path"] == "plan.md"
    assert body["size"] == len("# plan\nstep one")


async def test_get_file_rejects_path_escape(client, session_with_files):
    sid, _ = session_with_files
    r = await client.get(
        f"/api/sessions/{sid}/files",
        params={"path": "../outside.md"},
        headers=HEADERS,
    )
    assert r.status_code == 403


async def test_get_file_missing_returns_404(client, session_with_files):
    sid, _ = session_with_files
    r = await client.get(
        f"/api/sessions/{sid}/files",
        params={"path": "no-such.md"},
        headers=HEADERS,
    )
    assert r.status_code == 404


async def test_get_file_oversize_returns_413(client, session_with_files):
    sid, _ = session_with_files
    r = await client.get(
        f"/api/sessions/{sid}/files",
        params={"path": "huge.md"},
        headers=HEADERS,
    )
    assert r.status_code == 413


async def test_get_file_unsupported_type_returns_415(client, session_with_files):
    sid, _ = session_with_files
    r = await client.get(
        f"/api/sessions/{sid}/files",
        params={"path": "blob.bin"},
        headers=HEADERS,
    )
    assert r.status_code == 415


async def test_get_file_token_query_param_works(client, session_with_files):
    """<img src> / <iframe src> path: no Authorization header, token in URL."""
    sid, _ = session_with_files
    r = await client.get(
        f"/api/sessions/{sid}/files",
        params={"path": "plan.md", "token": TOKEN},
    )
    assert r.status_code == 200


async def test_get_file_requires_auth(client, session_with_files):
    sid, _ = session_with_files
    r = await client.get(
        f"/api/sessions/{sid}/files",
        params={"path": "plan.md"},
    )
    assert r.status_code == 401


async def test_get_file_session_not_found(client):
    r = await client.get(
        "/api/sessions/no-such-session/files",
        params={"path": "x.md"},
        headers=HEADERS,
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/sessions/{id}/showme/resolve — credential normalization
# ---------------------------------------------------------------------------
#
# Pins the fix for the credential-shape bug: an attached credential must
# reach the harness as a resolved `HarnessCredential`, never a raw DB row
# (which is encrypted, missing home_dir for Codex, and crashes attribute
# access in `_apply_env_credential` / `_apply_home_dir`). The route goes
# through `session_manager.resolve_credential_by_id`, which normalizes for
# us.


async def _patch_showme(monkeypatch):
    """Capture the credential argument the resolver was called with."""
    captured: dict = {}

    async def _fake_resolve(text, *, harness, model, credential, working_dir, messages, session_name=None):
        captured["credential"] = credential
        captured["model"] = model
        captured["working_dir"] = working_dir
        from server.showme_ai import ShowMeResolution

        return ShowMeResolution(path="answer.md")

    monkeypatch.setattr("server.routers.files.resolve_showme_reference", _fake_resolve)
    return captured


async def test_showme_resolve_no_credential_passes_none(
    client, session_with_files, monkeypatch
):
    captured = await _patch_showme(monkeypatch)
    sid, _ = session_with_files

    r = await client.post(
        f"/api/sessions/{sid}/showme/resolve",
        json={"text": "the plan"},
        headers=HEADERS,
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"path": "answer.md", "message": None}
    # No credential on the agent → resolver should receive None (the CLI
    # falls back to whatever host auth it finds).
    assert captured["credential"] is None


async def test_showme_resolve_attached_credential_is_normalized(
    client, session_with_files, monkeypatch
):
    """Regression guard: a session whose effective agent has an attached
    credential must trigger the credential resolver, so the harness receives
    a `HarnessCredential` (decrypted, right shape) rather than the raw DB row.
    """
    from datetime import datetime, timezone

    from server.crypto import encrypt
    from server.harness.events import HarnessCredential

    sid, _ = session_with_files
    db = session_manager.db
    now = datetime.now(timezone.utc).isoformat()
    secret = encrypt("sk-ant-test", TOKEN)
    await db.save_credential(
        credential_id="cred-1",
        backend="claude-code",
        label="test key",
        auth_type="api_key",
        secret_encrypted=secret,
        created_at=now,
    )
    # Attach the credential at the agent level — matches the production path
    # the bug was filed against.
    agent = await db.get_system_agent()
    await db.update_agent(agent["id"], credential_id="cred-1")

    captured = await _patch_showme(monkeypatch)
    r = await client.post(
        f"/api/sessions/{sid}/showme/resolve",
        json={"text": "the plan"},
        headers=HEADERS,
    )
    assert r.status_code == 200, r.text

    cred = captured["credential"]
    assert isinstance(cred, HarnessCredential), (
        f"resolver got {type(cred).__name__} — bug: raw DB row reaches the harness"
    )
    assert cred.backend == "claude-code"
    assert cred.auth_type == "api_key"
    # Secret must be the decrypted plaintext, not the on-disk ciphertext.
    assert cred.secret == "sk-ant-test"


