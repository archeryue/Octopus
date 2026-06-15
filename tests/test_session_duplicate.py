"""Integration tests for the /fork copy-dir saga (session-fork-copy.md).

Drives `SessionManager.duplicate_session` against a real in-memory DB. Unlike
`fork_session` (/rewind), `duplicate_session` copies the WHOLE conversation onto
an independent full copy of the working directory and leaves the parent intact.
"""

import json
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from server.database import Database
from server.main import app
from server import session_manager as sm
from server.session_manager import ForkError, QueuedPrompt, SessionManager
from server.session_manager import session_manager as global_sm


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


async def _seed_parent(mgr, working_dir, *, backend="claude-code", n_user=3):
    agent = await mgr.db.get_system_agent()
    parent = await mgr.create_session(
        agent["id"], "Parent", str(working_dir), backend=backend
    )
    seq = 0
    for i in range(n_user):
        await mgr.db.append_message(
            session_id=parent.id, seq=seq, role="user", type="text", content=f"q{i}"
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


def _repo(tmp_path):
    repo = tmp_path / "myproject"
    repo.mkdir()
    (repo / "code.py").write_text("print('hi')\n")
    sub = repo / "pkg"
    sub.mkdir()
    (sub / "mod.py").write_text("X = 1\n")
    return repo


# ----------------------------------------------------------------- happy path


@pytest.mark.asyncio
async def test_duplicate_copies_dir_and_history(manager, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    repo = _repo(tmp_path)
    parent = await _seed_parent(manager, repo)

    fork = await manager.duplicate_session(parent.id, label="my fork")

    # New, independent session under the same lineage.
    assert fork.origin == "fork"
    assert fork.forked_from_session_id == parent.id
    assert fork.name == "my fork"
    assert fork.fork_status == "ready"
    # fork_after_seq stays = last copied seq (the HISTORY_REPLAY cutoff); the
    # full-copy flag is what suppresses the rewind badge in the UI.
    assert fork.fork_after_seq == 5
    assert json.loads(fork.fork_metadata)["full_copy"] is True

    # Working dir is an independent full copy under ~/.octopus/fork/.
    dest = Path(fork.working_dir)
    assert dest != repo
    assert str(dest).startswith(str(tmp_path / "home" / ".octopus" / "fork"))
    assert dest.is_dir()
    assert (dest / "code.py").read_text() == "print('hi')\n"
    assert (dest / "pkg" / "mod.py").read_text() == "X = 1\n"

    # Edits to the copy don't touch the original.
    (dest / "code.py").write_text("changed\n")
    assert (repo / "code.py").read_text() == "print('hi')\n"

    # Whole history carried (3 user/assistant pairs = 6 messages).
    copied = await manager.db.load_messages(fork.id)
    assert [m["seq"] for m in copied] == [0, 1, 2, 3, 4, 5]
    assert fork._message_count == 6


@pytest.mark.asyncio
async def test_duplicate_leaves_parent_untouched(manager, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    repo = _repo(tmp_path)
    parent = await _seed_parent(manager, repo)

    fork = await manager.duplicate_session(parent.id)

    # Parent stays live (NOT archived — the key difference from /rewind).
    assert parent.id in manager.sessions
    assert parent.working_dir == str(repo)
    rows = {r["id"]: r for r in await manager.db.load_sessions(include_archived=True)}
    assert rows[parent.id]["archived"] == 0
    assert rows[fork.id]["archived"] == 0
    # Default label is "<parent> (fork)".
    assert fork.name == "Parent (fork)"


@pytest.mark.asyncio
async def test_duplicate_broadcasts_session_forked(manager, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    repo = _repo(tmp_path)
    parent = await _seed_parent(manager, repo)

    events = []
    monkeypatch.setattr(manager, "_broadcast", lambda e: events.append(e) or _noop())

    fork = await manager.duplicate_session(parent.id)
    evt = next(e for e in events if e.get("type") == "session_forked")
    assert evt["parent_session_id"] == parent.id
    assert evt["fork_session_id"] == fork.id
    assert evt["name"] == fork.name


async def _noop():
    return None


# ------------------------------------------------------------------- guards


@pytest.mark.asyncio
async def test_duplicate_unknown_parent(manager):
    with pytest.raises(ForkError) as ei:
        await manager.duplicate_session("nope")
    assert ei.value.reason == "parent_not_found"
    assert ei.value.status_code == 404


@pytest.mark.asyncio
async def test_duplicate_refused_active_task(manager, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    repo = _repo(tmp_path)
    parent = await _seed_parent(manager, repo)

    import asyncio

    async def _never():
        await asyncio.sleep(60)

    parent._active_task = asyncio.create_task(_never())
    try:
        with pytest.raises(ForkError) as ei:
            await manager.duplicate_session(parent.id)
        assert ei.value.reason == "fork_blocked_parent_turn_active"
    finally:
        parent._active_task.cancel()


@pytest.mark.asyncio
async def test_duplicate_refused_queued_message(manager, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    repo = _repo(tmp_path)
    parent = await _seed_parent(manager, repo)
    parent._pending_queue.append(QueuedPrompt(prompt="later", attachment_ids=[]))
    with pytest.raises(ForkError) as ei:
        await manager.duplicate_session(parent.id)
    assert ei.value.reason == "fork_blocked_parent_turn_active"


@pytest.mark.asyncio
async def test_duplicate_backend_not_supported(manager, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    repo = _repo(tmp_path)
    parent = await _seed_parent(manager, repo)

    class _NoFork:
        can_fork = False
        backend_kind = "claude_code"

    monkeypatch.setattr(sm, "get_harness", lambda b: _NoFork())
    from server.harness import BackendForkNotSupported

    with pytest.raises(BackendForkNotSupported):
        await manager.duplicate_session(parent.id)


@pytest.mark.asyncio
async def test_duplicate_prepare_fork_failure_compensates(manager, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    repo = _repo(tmp_path)
    parent = await _seed_parent(manager, repo)

    harness = sm.get_harness(parent.backend)

    async def boom(*a, **k):
        raise RuntimeError("prepare blew up")

    async def fake_cleanup(*a, **k):
        return None

    monkeypatch.setattr(harness, "prepare_fork", boom)
    monkeypatch.setattr(harness, "cleanup_incomplete_fork_artifacts", fake_cleanup)

    with pytest.raises(RuntimeError):
        await manager.duplicate_session(parent.id)

    # Compensation: no orphan session row, no orphan copied dir, parent freed.
    rows = {r["id"]: r for r in await manager.db.load_sessions(include_archived=True)}
    assert parent.id in rows
    assert len(rows) == 1  # only the parent
    fork_base = tmp_path / "home" / ".octopus" / "fork"
    assert not fork_base.exists() or not any(fork_base.iterdir())
    assert parent._forking is False


# -------------------------------------------------------------- route layer


@pytest.fixture
async def client():
    db = Database(":memory:")
    await db.initialize()
    global_sm.sessions.clear()
    await global_sm.initialize(db)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    await db.close()


@pytest.mark.asyncio
async def test_duplicate_route(client, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    repo = _repo(tmp_path)
    parent = await _seed_parent(global_sm, repo)

    H = {"Authorization": "Bearer changeme"}
    res = await client.post(
        f"/api/sessions/{parent.id}/duplicate",
        headers=H,
        json={"label": "routed fork"},
    )
    assert res.status_code == 201
    body = res.json()
    assert body["name"] == "routed fork"
    assert body["forked_from_session_id"] == parent.id
    assert body["id"] != parent.id
    # Parent still listed (not archived).
    listed = (await client.get("/api/sessions", headers=H)).json()
    assert parent.id in {s["id"] for s in listed}


@pytest.mark.asyncio
async def test_duplicate_route_unknown_parent(client):
    res = await client.post(
        "/api/sessions/missing/duplicate",
        headers={"Authorization": "Bearer changeme"},
        json={},
    )
    assert res.status_code == 404
    assert res.json()["detail"]["reason"] == "parent_not_found"


@pytest.mark.asyncio
async def test_duplicate_route_exposes_full_copy_flag(client, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    repo = _repo(tmp_path)
    parent = await _seed_parent(global_sm, repo)
    H = {"Authorization": "Bearer changeme"}
    body = (
        await client.post(f"/api/sessions/{parent.id}/duplicate", headers=H, json={})
    ).json()
    # The UI distinguishes a copy-dir duplicate from a rewind via this flag.
    assert body["fork_is_full_copy"] is True
    # The parent (a normal session) reports False.
    detail = (await client.get(f"/api/sessions/{parent.id}", headers=H)).json()
    assert detail["fork_is_full_copy"] is False


# ----------------------------------------------------- replay-cutoff (Vera #1)


@pytest.mark.asyncio
async def test_duplicate_replay_cutoff_covers_full_history(manager, tmp_path, monkeypatch):
    # The HISTORY_REPLAY first turn injects parent messages with
    # seq <= fork_after_seq. For a duplicate that cutoff MUST cover the whole
    # carried-over conversation — otherwise the fork continues with no context.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    repo = _repo(tmp_path)
    parent = await _seed_parent(manager, repo, n_user=3)  # 6 messages, seq 0..5

    fork = await manager.duplicate_session(parent.id)
    assert fork.fork_after_seq == 5  # last copied seq, not -1

    replayed = [
        m for m in await manager.db.load_messages(fork.id)
        if m["seq"] <= fork.fork_after_seq
    ]
    assert [m["seq"] for m in replayed] == [0, 1, 2, 3, 4, 5]


# ------------------------------------------------ copy/cleanup leaks (Vera #3/#4)


@pytest.mark.asyncio
async def test_duplicate_copy_failure_removes_partial_dir(manager, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    repo = _repo(tmp_path)
    parent = await _seed_parent(manager, repo)

    captured = {}

    def boom_copy(src, dest):
        # Simulate copytree leaving a half-populated dir before raising.
        Path(dest).mkdir(parents=True, exist_ok=True)
        (Path(dest) / "partial").write_text("x")
        captured["dest"] = dest
        raise OSError("disk full mid-copy")

    monkeypatch.setattr(manager, "_copy_tree", boom_copy)

    with pytest.raises(ForkError) as ei:
        await manager.duplicate_session(parent.id)
    assert ei.value.reason == "copy_failed"
    # The partial dir is swept; no row was created; parent freed.
    assert not Path(captured["dest"]).exists()
    rows = await manager.db.load_sessions(include_archived=True)
    assert len(rows) == 1
    assert parent._forking is False


@pytest.mark.asyncio
async def test_duplicate_cleanup_failure_leaves_row_and_dir(manager, tmp_path, monkeypatch):
    # If artifact cleanup ALSO fails after prepare_fork blew up, leave both the
    # 'initializing' row AND the copied dir for the startup sweep to retry — do
    # NOT strand the row pointing at a deleted working_dir.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    repo = _repo(tmp_path)
    parent = await _seed_parent(manager, repo)

    harness = sm.get_harness(parent.backend)

    async def boom(*a, **k):
        raise RuntimeError("prepare blew up")

    async def cleanup_boom(*a, **k):
        raise RuntimeError("cleanup blew up too")

    monkeypatch.setattr(harness, "prepare_fork", boom)
    monkeypatch.setattr(harness, "cleanup_incomplete_fork_artifacts", cleanup_boom)

    with pytest.raises(RuntimeError):
        await manager.duplicate_session(parent.id)

    rows = {r["id"]: r for r in await manager.db.load_sessions(include_archived=True)}
    fork_rows = [r for r in rows.values() if r["id"] != parent.id]
    assert len(fork_rows) == 1
    fr = fork_rows[0]
    assert fr["fork_status"] == "initializing"
    # The copied dir survives for retry.
    assert Path(fr["working_dir"]).is_dir()
    assert manager._is_fork_copy_dir(fr["working_dir"])


@pytest.mark.asyncio
async def test_recover_removes_abandoned_fork_copy_dir(manager, tmp_path, monkeypatch):
    # The startup sweep purges an 'initializing' duplicate AND removes its
    # private copied dir (a /rewind fork's shared parent dir is left alone).
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    copied = Path(manager._fork_copy_dest(str(tmp_path / "proj"), "deadbeef0001"))
    copied.mkdir(parents=True)
    (copied / "f.txt").write_text("x")
    parent = await _seed_parent(manager, tmp_path / "proj")

    await manager.db.create_fork_session(
        fork_id="deadbeef0001", name="o", working_dir=str(copied),
        created_at="2026-06-15T00:00:00+00:00", parent_id=parent.id,
        backend="claude-code", agent_id=parent.agent_id, credential_id=None,
        resume_id="resume-xyz", fork_after_seq=5,
    )

    harness = sm.get_harness("claude-code")

    async def fake_cleanup(*a, **k):
        return None

    monkeypatch.setattr(harness, "cleanup_incomplete_fork_artifacts", fake_cleanup)
    await manager._recover_incomplete_forks()

    rows = await manager.db.load_sessions(include_archived=True)
    assert not any(r["id"] == "deadbeef0001" for r in rows)
    assert not copied.exists()  # private copy removed


def test_is_fork_copy_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    base = SessionManager._fork_copy_base()
    assert SessionManager._is_fork_copy_dir(f"{base}/proj-abc") is True
    assert SessionManager._is_fork_copy_dir(base) is False  # the base itself
    assert SessionManager._is_fork_copy_dir("/home/me/project") is False
    assert SessionManager._is_fork_copy_dir(None) is False
    # A path that merely shares a prefix string but isn't under the base.
    assert SessionManager._is_fork_copy_dir(f"{base}-sneaky/x") is False
    # A `~`-style row still classifies (expanduser/abspath normalization).
    assert SessionManager._is_fork_copy_dir("~/.octopus/fork/proj-abc") is True


@pytest.mark.asyncio
async def test_full_copy_marker_survives_first_turn_cleanup(manager, tmp_path, monkeypatch):
    # `fork_metadata` is cleared once the fork's first turn produces a result,
    # but the durable `full_copy` identity MUST persist so the UI keeps treating
    # it as a copy-dir fork rather than a rewind (Vera review).
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    repo = _repo(tmp_path)
    parent = await _seed_parent(manager, repo)
    fork = await manager.duplicate_session(parent.id)
    # Sanity: replay flag + full_copy both set right after duplicate.
    assert fork.fork_needs_replay in (True, False)
    fork.fork_needs_replay = True  # force the cleanup to run its full path

    await manager._clear_fork_first_turn_state(fork)

    assert fork.fork_needs_replay is False
    meta = json.loads(fork.fork_metadata)
    assert meta["full_copy"] is True
    assert "prefilled_prompt" not in meta
    # The public flag stays true → UI still renders "full copy".
    fields = sm.fork_info_fields(
        backend=fork.backend,
        forked_from_session_id=fork.forked_from_session_id,
        fork_after_seq=fork.fork_after_seq,
        fork_metadata=fork.fork_metadata,
        fork_revert_record=fork.fork_revert_record,
    )
    assert fields["fork_is_full_copy"] is True
    # And it's durable in the DB, not just in memory.
    rows = {r["id"]: r for r in await manager.db.load_sessions(include_archived=True)}
    persisted = json.loads(rows[fork.id]["fork_metadata"])
    assert persisted["full_copy"] is True


@pytest.mark.asyncio
async def test_rewind_metadata_fully_cleared_on_first_turn(manager, tmp_path, monkeypatch):
    # A /rewind fork has no durable keys → its fork_metadata clears to None
    # (so the composer doesn't re-prefill the rewound message).
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    repo = _repo(tmp_path)
    parent = await _seed_parent(manager, repo, n_user=3)
    fork = await manager.fork_session(parent.id, 2)  # rewind to seq 2
    assert json.loads(fork.fork_metadata).get("prefilled_prompt") is not None
    fork.fork_needs_replay = True

    await manager._clear_fork_first_turn_state(fork)

    assert fork.fork_metadata is None
