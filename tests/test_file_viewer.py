"""Tests for the in-app file viewer.

Covers:
  * `server.file_viewer.resolve_safe_path` — the single security gate
    every viewer entry point funnels through. The most important tests
    are the negative ones (escape, symlink escape, missing, oversize).
  * `GET /api/sessions/{id}/files{,/meta}` — wired endpoint, including
    auth (header + query-token both accepted) and proper status codes
    for each FileViewerError subclass.
  * `server.mcp_servers.viewer.show_file` — the model-facing tool. We
    invoke its underlying callable directly so we don't have to spin
    up a real MCP stdio server for unit tests.
"""

from __future__ import annotations

import os
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

    sess = await session_manager.create_session(
        "viewer-test", working_dir=str(root)
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
# MCP show_file tool (call the underlying function, no stdio loop needed)
# ---------------------------------------------------------------------------


def _call_show_file(working_dir: Path, path: str) -> str:
    """Invoke the show_file tool function with OCTOPUS_WORKING_DIR set.

    We grab the underlying callable off the FastMCP instance so we
    don't have to spin up a real MCP stdio server for a unit test.
    """
    from server.mcp_servers.viewer import show_file as tool_fn

    prev = os.environ.get("OCTOPUS_WORKING_DIR")
    os.environ["OCTOPUS_WORKING_DIR"] = str(working_dir)
    try:
        # FastMCP wraps the function; on older versions it's exposed as
        # the function directly. Try calling it; if that fails, drill
        # into .fn (FastMCP's wrapper attribute).
        if callable(tool_fn):
            try:
                return tool_fn(path)  # type: ignore[arg-type]
            except TypeError:
                pass
        return tool_fn.fn(path)  # type: ignore[attr-defined]
    finally:
        if prev is None:
            os.environ.pop("OCTOPUS_WORKING_DIR", None)
        else:
            os.environ["OCTOPUS_WORKING_DIR"] = prev


def test_mcp_show_file_success(tmp_path):
    (tmp_path / "ok.md").write_text("# ok")
    out = _call_show_file(tmp_path, "ok.md")
    assert "Opened ok.md" in out
    assert "markdown" in out


def test_mcp_show_file_escape_path(tmp_path):
    out = _call_show_file(tmp_path, "../etc/passwd")
    assert "Could not open" in out
    assert "escapes" in out.lower()


def test_mcp_show_file_missing(tmp_path):
    out = _call_show_file(tmp_path, "nope.md")
    assert "Could not open" in out
    assert "not found" in out.lower()


def test_mcp_show_file_no_working_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("OCTOPUS_WORKING_DIR", raising=False)
    from server.mcp_servers.viewer import show_file as tool_fn

    try:
        out = tool_fn("anything.md")  # type: ignore[arg-type]
    except TypeError:
        out = tool_fn.fn("anything.md")  # type: ignore[attr-defined]
    assert "misconfigured" in out.lower()


# ---------------------------------------------------------------------------
# ClaudeCodeBackend.build_args registers the viewer MCP server
# ---------------------------------------------------------------------------


def test_rewrite_slash_commands_translates_showme():
    """The `claude` CLI eats anything starting with /<word> before it
    reaches the model. The rewrite must remove the leading slash so
    the model actually sees the request and calls show_file."""
    from server.session_manager import _rewrite_slash_commands

    out = _rewrite_slash_commands("/showme docs/plan.md")
    assert not out.startswith("/")
    assert "mcp__viewer__show_file" in out
    assert "'docs/plan.md'" in out


def test_rewrite_slash_commands_handles_leading_whitespace():
    from server.session_manager import _rewrite_slash_commands

    out = _rewrite_slash_commands("  /showme README\n")
    assert "mcp__viewer__show_file" in out
    assert "'README'" in out


def test_rewrite_slash_commands_bare_showme_asks_for_arg():
    from server.session_manager import _rewrite_slash_commands

    out = _rewrite_slash_commands("/showme")
    assert not out.startswith("/")
    assert "which file" in out.lower()


def test_rewrite_slash_commands_passes_through_plain_text():
    from server.session_manager import _rewrite_slash_commands

    text = "Please open the file plan.md for me"
    assert _rewrite_slash_commands(text) == text


def test_rewrite_slash_commands_ignores_unrelated_slash_command():
    """We only rewrite /showme. Other slash commands fall through and
    the CLI handles them (or rejects them, which is fine)."""
    from server.session_manager import _rewrite_slash_commands

    assert _rewrite_slash_commands("/help") == "/help"


def test_build_args_injects_viewer_mcp_config(tmp_path):
    """The viewer is the whole point of the change; make sure we don't
    silently lose the flag on a future refactor."""
    import json as _json

    from server.backends.claude_code import ClaudeCodeBackend

    backend = ClaudeCodeBackend()
    argv, spawn = backend.build_args(
        prompt="hi", working_dir=str(tmp_path), resume_id=None, credential=None
    )
    assert "--mcp-config" in argv
    cfg_idx = argv.index("--mcp-config") + 1
    cfg = _json.loads(argv[cfg_idx])
    assert "viewer" in cfg["mcpServers"]
    assert cfg["mcpServers"]["viewer"]["env"]["OCTOPUS_WORKING_DIR"] == str(tmp_path)

    assert "--append-system-prompt" in argv
    sp_idx = argv.index("--append-system-prompt") + 1
    assert "show_file" in argv[sp_idx]
    assert "/showme" in argv[sp_idx]
