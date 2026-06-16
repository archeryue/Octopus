"""Unit tests for the full-copy fork strategy (session-fork.md): each
harness copies its NATIVE transcript into a fresh resume id so a `/fork`
duplicate continues with real context instead of replaying history.

These exercise the pure filesystem mechanics (no real CLI) — copy + id/cwd
rewrite + fallback + cleanup — for both backends. Real CLI copy->resume->recall
lives in test_fork_native_copy_real.py (gated)."""

import json
from pathlib import Path

import pytest

from server.harness.events import HarnessCredential
from server.harness import claude_code as cc
from server.harness import codex as cx


# ----------------------------------------------------------------- Claude

def test_claude_project_slug_matches_cli(monkeypatch, tmp_path):
    # Pin the slug to the REAL CLI behavior (verified by the gated real test):
    # every non-alphanumeric char -> '-', case preserved, runs NOT collapsed.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    base = tmp_path / ".claude" / "projects"
    cases = {
        # underscores AND the trailing -re0 from pytest tmp dirs
        "/tmp/pytest-of-x/pytest-99/test_claude_native_copy_re0/src":
            "-tmp-pytest-of-x-pytest-99-test-claude-native-copy-re0-src",
        # dotfile dir -> double dash, case preserved, no collapse
        "/home/u/.octopus/fork/Octopus-a0434":
            "-home-u--octopus-fork-Octopus-a0434",
    }
    for wd, slug in cases.items():
        assert cc._claude_project_dir(wd) == base / slug


def _write_claude_transcript(base_home, working_dir, sid):
    """Seed a minimal real-shape Claude transcript under <home>/projects/<slug>."""
    d = cc._claude_project_dir(working_dir)
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{sid}.jsonl"
    lines = [
        {"type": "operation", "sessionId": sid, "content": "x"},
        {"type": "user", "sessionId": sid, "cwd": working_dir, "message": "hi"},
        {"type": "assistant", "sessionId": sid, "cwd": working_dir, "message": "yo"},
    ]
    f.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    return f


@pytest.mark.asyncio
async def test_claude_fork_copy_rewrites_and_resumes(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    parent_wd, dest_wd = "/proj/parent", "/home/u/.octopus/fork/parent-abc"
    _write_claude_transcript(tmp_path, parent_wd, "pid-1")

    art = await cc._fork_copy(
        parent_working_dir=parent_wd, parent_resume_id="pid-1",
        dest_working_dir=dest_wd, new_resume_id="nid-2",
    )
    assert art.resume_id == "nid-2"
    assert art.needs_replay is False

    dest = cc._claude_project_dir(dest_wd) / "nid-2.jsonl"
    assert dest.is_file()
    rows = [json.loads(l) for l in dest.read_text().splitlines() if l]
    # Every sessionId rewritten; every cwd repointed to the copied dir.
    assert {r["sessionId"] for r in rows} == {"nid-2"}
    assert {r["cwd"] for r in rows if "cwd" in r} == {dest_wd}


@pytest.mark.asyncio
async def test_claude_fork_copy_fallback_when_no_transcript(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    # No parent resume id → replay fallback.
    art = await cc._fork_copy(
        parent_working_dir="/p", parent_resume_id=None,
        dest_working_dir="/d", new_resume_id="n",
    )
    assert art.resume_id is None and art.needs_replay is True
    # Resume id present but file missing → still fallback.
    art2 = await cc._fork_copy(
        parent_working_dir="/p", parent_resume_id="ghost",
        dest_working_dir="/d", new_resume_id="n",
    )
    assert art2.needs_replay is True


@pytest.mark.asyncio
async def test_claude_fork_cleanup_reraises_on_oserror(tmp_path, monkeypatch):
    # A real removal failure (not "already gone") must RE-RAISE so the saga
    # keeps the row for a retry instead of stranding the transcript (Vera).
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    dest_wd = "/x/y"
    d = cc._claude_project_dir(dest_wd)
    d.mkdir(parents=True, exist_ok=True)
    (d / "nid.jsonl").mkdir()  # a dir → unlink raises IsADirectoryError (OSError)
    with pytest.raises(OSError):
        await cc._fork_cleanup(dest_wd, "nid", "fork1")


@pytest.mark.asyncio
async def test_claude_fork_cleanup_removes_copy(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    dest_wd = "/home/u/.octopus/fork/x-1"
    d = cc._claude_project_dir(dest_wd)
    d.mkdir(parents=True, exist_ok=True)
    (d / "nid.jsonl").write_text("{}\n")
    await cc._fork_cleanup(dest_wd, "nid", "fork1")
    assert not (d / "nid.jsonl").exists()
    # Idempotent + no-op for replay (resume id None).
    await cc._fork_cleanup(dest_wd, "nid", "fork1")
    await cc._fork_cleanup(dest_wd, None, "fork1")


# ----------------------------------------------------------------- Codex

def _write_codex_rollout(home_dir, rid):
    sess = Path(home_dir) / "sessions" / "2026" / "06" / "15"
    sess.mkdir(parents=True, exist_ok=True)
    f = sess / f"rollout-2026-06-15T00-00-00-{rid}.jsonl"
    lines = [
        {"type": "session_meta", "payload": {"id": rid, "cwd": "/whatever"}},
        {"type": "response_item", "payload": {"text": "hello"}},
    ]
    f.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    return f


@pytest.mark.asyncio
async def test_codex_fork_copy_rewrites_session_meta(tmp_path):
    home = tmp_path / "codexhome"
    home.mkdir()
    _write_codex_rollout(home, "rid-1")
    cred = HarnessCredential(backend="codex", auth_type="oauth", home_dir=str(home))

    art = await cx._fork_copy(
        parent_working_dir="/x", parent_resume_id="rid-1",
        parent_credential=cred, dest_working_dir="/y", new_resume_id="rid-2",
    )
    assert art.resume_id == "rid-2"
    assert art.needs_replay is False

    copied = cx._find_rollout(home / "sessions", "rid-2")
    assert copied is not None
    meta = json.loads(copied.read_text().splitlines()[0])
    assert meta["payload"]["id"] == "rid-2"


@pytest.mark.asyncio
async def test_codex_fork_copy_fallback_when_no_rollout(tmp_path):
    home = tmp_path / "codexhome"
    home.mkdir()
    cred = HarnessCredential(backend="codex", auth_type="oauth", home_dir=str(home))
    art = await cx._fork_copy(
        parent_working_dir="/x", parent_resume_id="missing",
        parent_credential=cred, dest_working_dir="/y", new_resume_id="n",
    )
    assert art.needs_replay is True
    art2 = await cx._fork_copy(
        parent_working_dir="/x", parent_resume_id=None,
        parent_credential=cred, dest_working_dir="/y", new_resume_id="n",
    )
    assert art2.needs_replay is True


@pytest.mark.asyncio
async def test_codex_find_rollout_confirms_session_meta(tmp_path):
    # A rollout whose FILENAME contains the id but whose session_meta.id differs
    # must NOT match — _find_rollout confirms session_meta before returning (it
    # drives deletion, so a filename-only hit could delete the wrong file).
    home = tmp_path / "codexhome"; home.mkdir()
    sess = home / "sessions" / "2026" / "06" / "15"
    sess.mkdir(parents=True)
    # filename says rid-2, content says rid-1
    f = sess / "rollout-2026-06-15T00-00-00-rid-2.jsonl"
    f.write_text(json.dumps({"type": "session_meta", "payload": {"id": "rid-1"}}) + "\n")
    assert cx._find_rollout(home / "sessions", "rid-2") is None
    assert cx._find_rollout(home / "sessions", "rid-1") == f


@pytest.mark.asyncio
async def test_codex_fork_cleanup_reraises_on_oserror(tmp_path, monkeypatch):
    home = tmp_path / "codexhome"; home.mkdir()
    _write_codex_rollout(home, "rid-1")
    cred = HarnessCredential(backend="codex", auth_type="oauth", home_dir=str(home))
    await cx._fork_copy(
        parent_working_dir="/x", parent_resume_id="rid-1",
        parent_credential=cred, dest_working_dir="/y", new_resume_id="rid-2",
    )

    def _boom(self, *a, **k):
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "unlink", _boom)
    with pytest.raises(OSError):
        await cx._fork_cleanup("/y", "rid-2", "fork1", credential=cred)


@pytest.mark.asyncio
async def test_codex_fork_cleanup_removes_copy(tmp_path):
    home = tmp_path / "codexhome"
    home.mkdir()
    _write_codex_rollout(home, "rid-1")
    cred = HarnessCredential(backend="codex", auth_type="oauth", home_dir=str(home))
    art = await cx._fork_copy(
        parent_working_dir="/x", parent_resume_id="rid-1",
        parent_credential=cred, dest_working_dir="/y", new_resume_id="rid-2",
    )
    assert cx._find_rollout(home / "sessions", "rid-2") is not None
    await cx._fork_cleanup("/y", "rid-2", "fork1", credential=cred)
    assert cx._find_rollout(home / "sessions", "rid-2") is None
    # The ORIGINAL rollout is untouched.
    assert cx._find_rollout(home / "sessions", "rid-1") is not None
