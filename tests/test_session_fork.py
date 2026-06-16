"""Integration tests for the fork saga (session-rewind.md §5.1).

Drives `SessionManager.fork_session` against a real in-memory DB with both
backends (Claude NATIVE_TRANSCRIPT synthesizing a JSONL under a temp HOME, and
Codex HISTORY_REPLAY), plus crash recovery, the `_forking` mutex, side-effect
classification routing, safe-revert, and dispatch-only replay wrapping.
"""

import json
import subprocess
from pathlib import Path

import pytest

from server.database import Database
from server.harness import BackendForkNotSupported, ForkArtifact, get_harness
from server.harness.events import HarnessEvent
from server import session_manager as sm
from server.session_manager import ForkError, QueuedPrompt, SessionManager
from server.delegations import DelegationRunState, delegation_manager


@pytest.fixture
async def manager():
    mgr = SessionManager()
    db = Database(":memory:")
    await db.initialize()
    await mgr.initialize(db)
    try:
        yield mgr
    finally:
        await db.close()


async def _seed_parent(mgr, *, backend="claude-code", working_dir="/repo",
                       n_user=3, git=(None, None)):
    """Parent with n_user user/assistant turn pairs; user rows carry the git
    anchor tuple so revert preflight has data."""
    agent = await mgr.db.get_system_agent()
    parent = await mgr.create_session(
        agent["id"], "Parent", working_dir, backend=backend
    )
    seq = 0
    for i in range(n_user):
        await mgr.db.append_message(
            session_id=parent.id, seq=seq, role="user", type="text",
            content=f"q{i}", git_head=git[0], git_status_clean=git[1],
        )
        seq += 1
        await mgr.db.append_message(
            session_id=parent.id, seq=seq, role="assistant", type="text",
            content=f"a{i}",
        )
        seq += 1
    await mgr.db.flush()
    parent._message_count = seq
    return parent


# ------------------------------------------------------------------ happy path


@pytest.mark.asyncio
async def test_fork_claude_history_replay(manager):
    # Claude forks use HISTORY_REPLAY (not native synth-resume — see
    # claude_code.py / Phase-5 finding), same contract as Codex.
    parent = await _seed_parent(manager, backend="claude-code")
    # Rewind to user message at seq=2 (the 2nd user turn). Copies seq < 2.
    fork = await manager.fork_session(parent.id, 2)

    assert fork.origin == "fork"
    assert fork.forked_from_session_id == parent.id
    assert fork.fork_after_seq == 1
    assert fork.fork_needs_replay is True  # replay on first turn
    assert fork.claude_session_id is None  # claude's own id arrives turn 1
    assert fork.fork_status == "ready"
    # Pi-style boundary: copied seq < 2 → 2 rows, next seq = 2.
    assert fork._message_count == 2
    copied = await manager.db.load_messages(fork.id)
    assert [m["seq"] for m in copied] == [0, 1]
    # Ephemeral metadata holds the prefilled prompt (parent's seq-2 text).
    meta = json.loads(fork.fork_metadata)
    assert meta["prefilled_prompt"] == "q1"  # seq 2 is the 2nd user turn ("q1")
    # Rewind, not branch: the fork inherits the parent's name and the parent
    # is archived so the fork takes its place in the active list.
    assert fork.name == parent.name == "Parent"
    assert parent.id not in manager.sessions
    rows = {r["id"]: r for r in await manager.db.load_sessions(include_archived=True)}
    assert rows[parent.id]["archived"] == 1
    assert rows[fork.id]["archived"] == 0


@pytest.mark.asyncio
async def test_fork_codex_history_replay(manager):
    parent = await _seed_parent(manager, backend="codex")
    fork = await manager.fork_session(parent.id, 2)
    assert fork.fork_needs_replay is True
    assert fork.claude_session_id is None  # resume id arrives on thread.started
    assert fork.fork_status == "ready"


@pytest.mark.asyncio
async def test_codex_fork_resume_id_null_survives_restart(manager):
    # Vera review BLOCKING #1: a HISTORY_REPLAY fork must NOT keep the
    # pre-minted resume-id hint in the DB, else a restart before the first
    # turn reloads the bogus id and spawns `codex resume <bogus>`.
    parent = await _seed_parent(manager, backend="codex")
    fork = await manager.fork_session(parent.id, 2)
    assert fork.claude_session_id is None
    rows = {r["id"]: r for r in await manager.db.load_sessions(include_archived=True)}
    assert rows[fork.id]["claude_session_id"] is None
    # Restart: reload over the same DB — still None.
    mgr2 = SessionManager()
    await mgr2.initialize(manager.db)
    assert mgr2.get_session(fork.id).claude_session_id is None
    assert mgr2.get_session(fork.id).fork_needs_replay is True


@pytest.mark.asyncio
async def test_fork_m0_empty(manager, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    parent = await _seed_parent(manager, backend="claude-code")
    fork = await manager.fork_session(parent.id, 0)
    assert fork.fork_after_seq == -1
    assert fork._message_count == 0
    assert await manager.db.load_messages(fork.id) == []
    meta = json.loads(fork.fork_metadata)
    assert meta["prefilled_prompt"] == "q0"  # the original first user message


@pytest.mark.asyncio
async def test_fork_m0_codex_still_wraps_replay(manager):
    parent = await _seed_parent(manager, backend="codex")
    fork = await manager.fork_session(parent.id, 0)
    # Even with an empty history, Codex forks need replay so the first-turn
    # shape stays uniform.
    assert fork.fork_needs_replay is True


@pytest.mark.asyncio
async def test_fork_of_fork(manager):
    parent = await _seed_parent(manager, backend="codex")
    fork = await manager.fork_session(parent.id, 2)
    # Give the fork some messages so it can be re-forked.
    await manager.db.append_message(session_id=fork.id, seq=2, role="user",
                                    type="text", content="fork-q")
    await manager.db.flush()
    fork._message_count = 3
    grandchild = await manager.fork_session(fork.id, 2)
    assert grandchild.forked_from_session_id == fork.id
    # Each fork archives its (now-replaced) parent — only the tip stays live.
    assert parent.id not in manager.sessions
    assert fork.id not in manager.sessions
    assert grandchild.id in manager.sessions


@pytest.mark.asyncio
async def test_fork_repoints_schedules_and_clears_bridge(manager):
    # Mirrors archive_session's tail: a `/schedule` anchored on the parent
    # follows onto the fork (the successor), while a bridge chat's sticky
    # pointer is cleared so its next message opens a fresh thread.
    parent = await _seed_parent(manager, backend="codex")
    await manager.db.save_schedule(
        schedule_id="sch1", agent_id=parent.agent_id, name="nightly",
        prompt="do it", created_at="2026-06-08T00:00:00+00:00",
        interval_seconds=3600, origin_session_id=parent.id,
    )
    await manager.db.save_bridge_mapping(
        platform="telegram", chat_id="c1", agent_id=parent.agent_id,
        session_id=parent.id,
    )

    fork = await manager.fork_session(parent.id, 2)

    schedules = {s["id"]: s for s in await manager.db.load_schedules()}
    assert schedules["sch1"]["origin_session_id"] == fork.id
    mappings = {m["chat_id"]: m for m in await manager.db.load_bridge_mappings()}
    assert mappings["c1"]["session_id"] is None


# ------------------------------------------------------------------ validation


@pytest.mark.asyncio
async def test_fork_rejects_unknown_seq(manager):
    parent = await _seed_parent(manager, backend="codex")
    with pytest.raises(ForkError) as ei:
        await manager.fork_session(parent.id, 999)
    assert ei.value.reason == "invalid_rewind_seq"


@pytest.mark.asyncio
async def test_fork_rejects_negative_seq(manager):
    parent = await _seed_parent(manager, backend="codex")
    with pytest.raises(ForkError) as ei:
        await manager.fork_session(parent.id, -1)
    assert ei.value.reason == "invalid_rewind_seq"


@pytest.mark.asyncio
async def test_fork_rejects_non_user_target(manager):
    parent = await _seed_parent(manager, backend="codex")
    with pytest.raises(ForkError) as ei:
        await manager.fork_session(parent.id, 1)  # seq 1 is an assistant msg
    assert ei.value.reason == "target_not_user_message"


@pytest.mark.asyncio
async def test_fork_unknown_parent(manager):
    with pytest.raises(ForkError) as ei:
        await manager.fork_session("nope", 0)
    assert ei.value.reason == "parent_not_found"


@pytest.mark.asyncio
async def test_fork_backend_not_supported(manager, monkeypatch):
    parent = await _seed_parent(manager, backend="codex")

    class _NoFork:
        can_fork = False

    monkeypatch.setattr(sm, "get_harness", lambda b: _NoFork())
    with pytest.raises(BackendForkNotSupported):
        await manager.fork_session(parent.id, 0)
    # No half-created row.
    rows = await manager.db.load_sessions(include_archived=True)
    assert not any(r["origin"] == "fork" for r in rows)


# ------------------------------------------------------------------ live-work refusal


@pytest.mark.asyncio
async def test_fork_refused_active_task(manager):
    parent = await _seed_parent(manager, backend="codex")
    import asyncio

    async def _never():
        await asyncio.sleep(60)

    parent._active_task = asyncio.create_task(_never())
    try:
        with pytest.raises(ForkError) as ei:
            await manager.fork_session(parent.id, 0)
        assert ei.value.reason == "fork_blocked_parent_turn_active"
    finally:
        parent._active_task.cancel()


@pytest.mark.asyncio
async def test_fork_refused_queued_message(manager):
    parent = await _seed_parent(manager, backend="codex")
    parent._pending_queue.append(QueuedPrompt(prompt="x", attachment_ids=[]))
    with pytest.raises(ForkError) as ei:
        await manager.fork_session(parent.id, 0)
    assert ei.value.reason == "fork_blocked_parent_turn_active"


@pytest.mark.asyncio
async def test_fork_refused_active_delegation(manager):
    parent = await _seed_parent(manager, backend="codex")
    rec = DelegationRunState(
        delegation_id="child1", parent_session_id=parent.id,
        target_agent_id="a", target_agent_name="A", request="do",
    )
    delegation_manager._records["child1"] = rec
    try:
        with pytest.raises(ForkError) as ei:
            await manager.fork_session(parent.id, 0)
        assert ei.value.reason == "fork_blocked_parent_turn_active"
    finally:
        delegation_manager._records.pop("child1", None)


@pytest.mark.asyncio
async def test_forking_flag_blocks_start_message(manager):
    parent = await _seed_parent(manager, backend="codex")
    parent._forking = True
    with pytest.raises(ValueError, match="busy"):
        await manager.start_message(parent.id, "hello")


# ------------------------------------------------------------------ saga compensation


@pytest.mark.asyncio
async def test_prepare_fork_failure_compensates(manager, monkeypatch):
    parent = await _seed_parent(manager, backend="claude-code")
    harness = get_harness("claude-code")
    cleaned = {}

    async def boom(*a, **k):
        raise RuntimeError("synthesis failed")

    async def fake_cleanup(working_dir, resume_id_hint, fork_id):
        cleaned["called"] = (working_dir, resume_id_hint, fork_id)

    monkeypatch.setattr(harness, "prepare_fork", boom)
    monkeypatch.setattr(harness, "cleanup_incomplete_fork_artifacts", fake_cleanup)

    with pytest.raises(RuntimeError):
        await manager.fork_session(parent.id, 2)
    # Cleanup ran FIRST, then the row was deleted — no orphan fork row.
    assert "called" in cleaned
    rows = await manager.db.load_sessions(include_archived=True)
    assert not any(r["origin"] == "fork" for r in rows)
    # `_forking` was released even on failure.
    assert parent._forking is False


@pytest.mark.asyncio
async def test_prepare_fork_failure_cleanup_raises_leaves_row(manager, monkeypatch):
    parent = await _seed_parent(manager, backend="claude-code")
    harness = get_harness("claude-code")

    async def boom(*a, **k):
        raise RuntimeError("synthesis failed")

    async def cleanup_boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(harness, "prepare_fork", boom)
    monkeypatch.setattr(harness, "cleanup_incomplete_fork_artifacts", cleanup_boom)

    with pytest.raises(OSError):
        await manager.fork_session(parent.id, 2)
    # Row stays as 'initializing' so the next boot retries idempotently.
    rows = await manager.db.load_incomplete_forks()
    assert len(rows) == 1 and rows[0]["fork_status"] == "initializing"
    assert parent._forking is False
    # …but the in-memory session is dropped so the failed fork can't appear
    # as a normal idle session (Vera review SHOULD-FIX #2).
    assert not any(s.origin == "fork" for s in manager.sessions.values())


# ------------------------------------------------------------------ crash recovery


@pytest.mark.asyncio
async def test_recover_purges_initializing(manager, monkeypatch):
    parent = await _seed_parent(manager, backend="claude-code")
    # Manually plant an 'initializing' fork row (saga crashed before step 7).
    await manager.db.create_fork_session(
        fork_id="orphan01", name="o", working_dir="/repo",
        created_at="2026-06-08T00:00:00+00:00", parent_id=parent.id,
        backend="claude-code", agent_id=parent.agent_id, credential_id=None,
        resume_id="resume-xyz", fork_after_seq=1,
    )
    harness = get_harness("claude-code")
    seen = {}

    async def fake_cleanup(working_dir, resume_id_hint, fork_id, *, credential=None):
        seen["args"] = (working_dir, resume_id_hint, fork_id)

    monkeypatch.setattr(harness, "cleanup_incomplete_fork_artifacts", fake_cleanup)
    await manager._recover_incomplete_forks()
    assert seen["args"] == ("/repo", "resume-xyz", "orphan01")
    rows = await manager.db.load_sessions(include_archived=True)
    assert not any(r["id"] == "orphan01" for r in rows)


@pytest.mark.asyncio
async def test_recover_keeps_initializing_when_cleanup_raises(manager, monkeypatch):
    parent = await _seed_parent(manager, backend="claude-code")
    await manager.db.create_fork_session(
        fork_id="orphan02", name="o", working_dir="/repo",
        created_at="2026-06-08T00:00:00+00:00", parent_id=parent.id,
        backend="claude-code", agent_id=parent.agent_id, credential_id=None,
        resume_id="r", fork_after_seq=1,
    )
    harness = get_harness("claude-code")

    async def cleanup_boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(harness, "cleanup_incomplete_fork_artifacts", cleanup_boom)
    await manager._recover_incomplete_forks()
    rows = await manager.db.load_incomplete_forks()
    assert any(r["id"] == "orphan02" for r in rows)  # still there for retry


@pytest.mark.asyncio
async def test_recover_finalizes_reverting(manager):
    parent = await _seed_parent(manager, backend="codex")
    await manager.db.create_fork_session(
        fork_id="rev01", name="o", working_dir="/repo",
        created_at="2026-06-08T00:00:00+00:00", parent_id=parent.id,
        backend="codex", agent_id=parent.agent_id, credential_id=None,
        resume_id=None, fork_after_seq=1,
    )
    await manager.db.update_session_field("rev01", fork_status="reverting")
    await manager._recover_incomplete_forks()
    rows = {r["id"]: r for r in await manager.db.load_sessions(include_archived=True)}
    row = rows["rev01"]
    assert row["fork_status"] == "ready"
    rec = json.loads(row["fork_revert_record"])
    assert rec["status"] == "unknown_post_crash"


# ------------------------------------------------------------------ persistence


@pytest.mark.asyncio
async def test_fork_metadata_survives_restart(manager, monkeypatch):
    parent = await _seed_parent(manager, backend="codex")
    fork = await manager.fork_session(parent.id, 2, label="My Fork")
    fork_id = fork.id
    # Simulate a restart: a fresh manager over the same DB.
    mgr2 = SessionManager()
    await mgr2.initialize(manager.db)
    reloaded = mgr2.get_session(fork_id)
    assert reloaded is not None
    meta = json.loads(reloaded.fork_metadata)
    assert meta["prefilled_prompt"] == "q1"
    assert meta["fork_label"] == "My Fork"
    # The public field surfaces the prefilled prompt.
    info = sm.fork_info_fields(
        backend=reloaded.backend,
        forked_from_session_id=reloaded.forked_from_session_id,
        fork_after_seq=reloaded.fork_after_seq,
        fork_metadata=reloaded.fork_metadata,
        fork_revert_record=reloaded.fork_revert_record,
    )
    assert info["fork_prefilled_prompt"] == "q1"


@pytest.mark.asyncio
async def test_unarchive_restores_fork_fields(manager):
    parent = await _seed_parent(manager, backend="codex")
    fork = await manager.fork_session(parent.id, 2)
    await manager.db.update_session_field(fork.id, archived=True)
    manager.sessions.pop(fork.id, None)
    restored = await manager.unarchive_session(fork.id)
    assert restored.forked_from_session_id == parent.id
    assert restored.fork_after_seq == 1
    assert restored.fork_metadata is not None


# ------------------------------------------------------------------ revert via saga


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.mark.asyncio
async def test_fork_with_revert_restores_files(manager, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "t")
    (repo / "a.py").write_text("v1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                          capture_output=True, text=True).stdout.strip()

    agent = await manager.db.get_system_agent()
    parent = await manager.create_session(agent["id"], "P", str(repo),
                                          backend="claude-code")
    # seq 0 user msg with clean git anchor at fork point; seq 1 the agent edits a.py.
    await manager.db.append_message(session_id=parent.id, seq=0, role="user",
                                    type="text", content="edit it",
                                    git_head=head, git_status_clean=True)
    await manager.db.append_message(session_id=parent.id, seq=1, role="assistant",
                                    type="tool_use", tool_name="Edit",
                                    tool_input={"file_path": str(repo / "a.py")},
                                    tool_use_id="e1")
    await manager.db.flush()
    parent._message_count = 2
    # The agent's edit is now on disk (dirty tree).
    (repo / "a.py").write_text("agent-edited\n")

    fork = await manager.fork_session(parent.id, 0, revert_files=True)
    rec = json.loads(fork.fork_revert_record)
    assert rec["status"] == "completed"
    assert (repo / "a.py").read_text() == "v1\n"   # restored to fork-point
    assert fork.fork_status == "ready"


@pytest.mark.asyncio
async def test_fork_with_revert_refused_non_git(manager, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    wd = tmp_path / "plain"
    wd.mkdir()
    agent = await manager.db.get_system_agent()
    parent = await manager.create_session(agent["id"], "P", str(wd),
                                          backend="claude-code")
    await manager.db.append_message(session_id=parent.id, seq=0, role="user",
                                    type="text", content="x")
    await manager.db.flush()
    parent._message_count = 1
    fork = await manager.fork_session(parent.id, 0, revert_files=True)
    rec = json.loads(fork.fork_revert_record)
    assert rec["status"] == "refused"
    # Fork still created successfully.
    assert fork.fork_status == "ready"


# ------------------------------------------------------------------ replay dispatch


class _FakeRun:
    """Records the dispatch prompt and emits a clean session_started+result."""

    def __init__(self):
        self.started_prompt = None

    async def start(self, prompt, working_dir, resume_id=None, credential=None):
        self.started_prompt = prompt

    async def stream(self):
        yield HarnessEvent(type="session_started", session_id="newthread")
        yield HarnessEvent(type="result", session_id="newthread")

    async def stop(self):
        pass

    async def interrupt(self):
        pass


@pytest.mark.asyncio
async def test_codex_first_turn_wraps_dispatch_only(manager, monkeypatch):
    parent = await _seed_parent(manager, backend="codex")
    fork = await manager.fork_session(parent.id, 2)
    assert fork.fork_needs_replay is True

    fake = _FakeRun()
    monkeypatch.setattr(manager, "_make_run", lambda *a, **k: fake)

    async for _ in manager.send_message(fork.id, "edited prompt"):
        pass

    # Dispatch prompt was wrapped with the fork history…
    assert "<fork-history" in fake.started_prompt
    assert "edited prompt" in fake.started_prompt
    # …but the PERSISTED user message is the raw text (dispatch-only).
    msgs = await manager.db.load_messages(fork.id)
    user_rows = [m for m in msgs if m["role"] == "user" and m["seq"] == 2]
    assert user_rows and user_rows[0]["content"] == "edited prompt"
    # First result cleared the ephemeral fork state.
    assert fork.fork_needs_replay is False
    assert fork.fork_metadata is None
    assert fork.claude_session_id == "newthread"


@pytest.mark.asyncio
async def test_codex_turn_two_not_wrapped(manager, monkeypatch):
    parent = await _seed_parent(manager, backend="codex")
    fork = await manager.fork_session(parent.id, 2)
    fake = _FakeRun()
    monkeypatch.setattr(manager, "_make_run", lambda *a, **k: fake)
    async for _ in manager.send_message(fork.id, "turn one"):
        pass
    # Turn 2: replay flag cleared → no wrap.
    fake2 = _FakeRun()
    monkeypatch.setattr(manager, "_make_run", lambda *a, **k: fake2)
    async for _ in manager.send_message(fork.id, "turn two"):
        pass
    assert "<fork-history" not in fake2.started_prompt
    assert fake2.started_prompt == "turn two"
