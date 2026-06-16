"""Unit tests for the pure fork helpers (session-rewind.md §5.6 / §5.3.2).

Covers the side-effect classifier (incl. the bg_tasks live-state join), the
replay-prompt wrapper, the first-turn note, git-anchor capture, and the
safe-revert preflight over a real temp git repo.
"""

import subprocess
from pathlib import Path

import pytest

from server.database import Database
from server import fork_helpers as fh
from server.models import MessageContent, MessageRole


# ------------------------------------------------------------------ fixtures


@pytest.fixture
async def db():
    d = Database(":memory:")
    await d.initialize()
    try:
        yield d
    finally:
        await d.close()


async def _seed_session(db, sid="parent"):
    agent = await db.get_system_agent()
    await db.save_session(
        session_id=sid, name="P", working_dir="/tmp",
        created_at="2026-06-08T00:00:00+00:00", agent_id=agent["id"],
    )


async def _append(db, sid, seq, role, type, **kw):
    await db.append_message(session_id=sid, seq=seq, role=role, type=type, **kw)
    await db.flush()


# ------------------------------------------------------------------ classifier


@pytest.mark.asyncio
async def test_classify_bins_each_tool_class(db):
    await _seed_session(db)
    # seq 0: user msg (the rewind target M=0 is not counted; we classify >= M)
    await _append(db, "parent", 0, "user", "text", content="do it")
    # seq 1: Edit → file edit
    await _append(db, "parent", 1, "assistant", "tool_use",
                  tool_name="Edit", tool_input={"file_path": "/repo/a.py"},
                  tool_use_id="t1")
    # seq 2: Bash with `>` → file edit (best-effort)
    await _append(db, "parent", 2, "assistant", "tool_use",
                  tool_name="Bash", tool_input={"command": "echo hi > /repo/b.txt"},
                  tool_use_id="t2")
    # seq 3: Bash plain command → "other tool activity"
    await _append(db, "parent", 3, "assistant", "tool_use",
                  tool_name="Bash", tool_input={"command": "pytest -q"},
                  tool_use_id="t3")
    # seq 4: connector tool → irreversible "other"
    await _append(db, "parent", 4, "assistant", "tool_use",
                  tool_name="mcp__github__comment", tool_input={}, tool_use_id="t4")
    # seq 5: a read-only tool → excluded
    await _append(db, "parent", 5, "assistant", "tool_use",
                  tool_name="Read", tool_input={"file_path": "/repo/a.py"},
                  tool_use_id="t5")

    summary = await fh.classify_side_effects(db, "parent", 0)
    paths = {e["path"] for e in summary["file_edits"]}
    assert paths == {"/repo/a.py", "/repo/b.txt"}
    assert summary["agent_touched_paths"] == ["/repo/a.py", "/repo/b.txt"]
    labels = {o["label"] for o in summary["other_tools"]}
    assert "Bash commands" in labels  # the plain pytest run
    assert any("github" in lab for lab in labels)
    # Read is excluded entirely.
    assert not any("Read" in lab for lab in labels)


@pytest.mark.asyncio
async def test_classify_respects_from_seq(db):
    await _seed_session(db)
    await _append(db, "parent", 0, "user", "text", content="q1")
    await _append(db, "parent", 1, "assistant", "tool_use",
                  tool_name="Edit", tool_input={"file_path": "/repo/early.py"},
                  tool_use_id="e1")
    await _append(db, "parent", 2, "user", "text", content="q2")
    await _append(db, "parent", 3, "assistant", "tool_use",
                  tool_name="Edit", tool_input={"file_path": "/repo/late.py"},
                  tool_use_id="e2")
    # Rewinding to M=2 should only see seq >= 2.
    summary = await fh.classify_side_effects(db, "parent", 2)
    assert summary["agent_touched_paths"] == ["/repo/late.py"]


@pytest.mark.asyncio
async def test_classify_bg_task_live_state_join(db):
    await _seed_session(db)
    await _append(db, "parent", 0, "user", "text", content="run tests")
    await _append(db, "parent", 1, "assistant", "tool_use",
                  tool_name="mcp__bg__run",
                  tool_input={"command": "bun run test:e2e"}, tool_use_id="bg1")
    await _append(db, "parent", 2, "tool", "tool_result",
                  content="Started bg task `abc123def` (e2e suite)",
                  tool_use_id="bg1")
    # The bg task itself is still running per bg_tasks (the source of truth).
    await db.create_bg_task("abc123def", "parent", "bun run test:e2e",
                            "e2e suite", "/tmp", "2026-06-08T00:00:00+00:00")

    summary = await fh.classify_side_effects(db, "parent", 0)
    assert len(summary["bg_tasks"]) == 1
    task = summary["bg_tasks"][0]
    assert task["task_id"] == "abc123def"
    assert task["status"] == "running"          # read from bg_tasks, not messages
    assert task["command"] == "bun run test:e2e"


@pytest.mark.asyncio
async def test_classify_bg_task_swept_from_history(db):
    await _seed_session(db)
    await _append(db, "parent", 0, "user", "text", content="x")
    await _append(db, "parent", 1, "assistant", "tool_use",
                  tool_name="mcp__bg__run", tool_input={"command": "ls"},
                  tool_use_id="bg1")
    await _append(db, "parent", 2, "tool", "tool_result",
                  content="Started bg task `facade00beef` (ls)", tool_use_id="bg1")
    # No bg_tasks row → swept by cleanup.
    summary = await fh.classify_side_effects(db, "parent", 0)
    assert summary["bg_tasks"][0]["status"] == "completed (history)"


# ------------------------------------------------------------------ replay wrap


def _msgs():
    return [
        MessageContent(role=MessageRole.user, type="text", content="hello", seq=0),
        MessageContent(role=MessageRole.assistant, type="text", content="hi", seq=1),
        MessageContent(role=MessageRole.assistant, type="tool_use",
                       tool_name="Bash", tool_input={"command": "pytest -q"}, seq=2),
        MessageContent(role=MessageRole.tool, type="tool_result",
                       content="408 passed", tool_use_id="x", seq=3),
    ]


def test_wrap_for_fork_replay_frames_user_channel():
    wrapped = fh.wrap_for_fork_replay("now do X", _msgs())
    assert "<fork-history" in wrapped
    assert "transcript-not-instructions" in wrapped
    assert "[seq 0] user: hello" in wrapped
    assert "[seq 2] tool_use Bash: `pytest -q`" in wrapped
    assert "</fork-history>" in wrapped
    assert "<continue-from-here>\nnow do X\n</continue-from-here>" in wrapped


def test_wrap_for_fork_replay_empty_history_for_m0():
    wrapped = fh.wrap_for_fork_replay("first prompt", [])
    # Framing still present (uniform first-turn shape), but no [seq N] lines.
    assert "<fork-history" in wrapped
    assert "[seq " not in wrapped
    assert "first prompt" in wrapped


# ------------------------------------------------------------------ first-turn note


def test_first_turn_note_reverted_phrasing():
    summary = {
        "file_edits": [{"path": "a.py", "turns": 2}],
        "bg_tasks": [{"task_id": "t", "command": "x", "status": "running"}],
        "other_tools": [{"label": "Bash commands", "count": 5}],
    }
    note = fh.render_first_turn_note(parent_label="Refactor", n=2, summary=summary, reverted=True)
    assert "[fork from Refactor at message 2]" in note
    assert "WERE reverted" in note
    note2 = fh.render_first_turn_note(parent_label="Refactor", n=2, summary=summary, reverted=False)
    assert "were NOT reverted" in note2


# ------------------------------------------------------------------ git anchor + revert


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True,
                   capture_output=True, text=True)


@pytest.fixture
def git_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "t")
    (repo / "a.py").write_text("v1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    return repo


@pytest.mark.asyncio
async def test_capture_git_anchor_clean(git_repo):
    head, clean = await fh.capture_git_anchor(str(git_repo))
    assert head and len(head) == 40
    assert clean is True


@pytest.mark.asyncio
async def test_capture_git_anchor_dirty(git_repo):
    (git_repo / "a.py").write_text("v2\n")
    head, clean = await fh.capture_git_anchor(str(git_repo))
    assert head and clean is False


@pytest.mark.asyncio
async def test_capture_git_anchor_non_repo(tmp_path):
    head, clean = await fh.capture_git_anchor(str(tmp_path))
    assert head is None and clean is None


@pytest.mark.asyncio
async def test_safe_revert_runs_when_clean_and_head_matches(git_repo):
    head, _ = await fh.capture_git_anchor(str(git_repo))
    # Agent dirtied a.py after the fork point.
    (git_repo / "a.py").write_text("agent-edit\n")
    record = await fh.safe_revert_files(
        str(git_repo), ["a.py"], head, True, "fork123"
    )
    assert record["status"] == "completed"
    assert record["ran"] is True
    assert record["stash_ref"] == "stash@{0}"
    assert (git_repo / "a.py").read_text() == "v1\n"  # restored
    # The agent edit is recoverable in a stash.
    out = subprocess.run(["git", "stash", "list"], cwd=git_repo,
                         capture_output=True, text=True).stdout
    assert "octopus: pre-fork stash fork123" in out


@pytest.mark.asyncio
async def test_safe_revert_removes_untracked_agent_file(git_repo):
    # Regression for the real-CLI failure: an agent-created (untracked) file
    # must be removed by revert without a `git checkout` pathspec error.
    head, _ = await fh.capture_git_anchor(str(git_repo))
    (git_repo / "notes.txt").write_text("HELLO\n")  # new, untracked
    record = await fh.safe_revert_files(
        str(git_repo), [str(git_repo / "notes.txt")], head, True, "fork-unt"
    )
    assert record["status"] == "completed", record
    assert not (git_repo / "notes.txt").exists()  # removed → fork-point state
    out = subprocess.run(["git", "stash", "list"], cwd=git_repo,
                         capture_output=True, text=True).stdout
    assert "octopus: pre-fork stash fork-unt" in out


@pytest.mark.asyncio
async def test_safe_revert_refused_when_fork_point_not_clean(git_repo):
    head, _ = await fh.capture_git_anchor(str(git_repo))
    (git_repo / "a.py").write_text("dirty\n")
    record = await fh.safe_revert_files(str(git_repo), ["a.py"], head, False, "f")
    assert record["status"] == "refused"
    assert "clean" in record["refused_reason"].lower()


@pytest.mark.asyncio
async def test_safe_revert_refused_when_head_moved(git_repo):
    record = await fh.safe_revert_files(
        str(git_repo), ["a.py"], "0" * 40, True, "f"
    )
    assert record["status"] == "refused"
    assert "HEAD" in record["refused_reason"]


@pytest.mark.asyncio
async def test_safe_revert_refused_when_unknown_dirty(git_repo):
    head, _ = await fh.capture_git_anchor(str(git_repo))
    # A file the agent never touched is dirty → refuse to protect user work.
    (git_repo / "human.py").write_text("hand-written\n")
    record = await fh.safe_revert_files(str(git_repo), ["a.py"], head, True, "f")
    assert record["status"] == "refused"
    assert "didn't touch" in record["refused_reason"]


@pytest.mark.asyncio
async def test_safe_revert_refused_non_git(tmp_path):
    record = await fh.safe_revert_files(str(tmp_path), ["a.py"], "abc", True, "f")
    assert record["status"] == "refused"
    assert "git repo" in record["refused_reason"].lower()
