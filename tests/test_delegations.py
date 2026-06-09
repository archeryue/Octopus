"""Tests for agent-to-agent delegation (agent-collaboration.md Phase 1).

Covers:
  * Schema migration adds `parent_session_id` (FK SET NULL on parent
    delete) and `delegation_request`; `origin='delegation'` round-trips.
  * `DelegationManager` agent-name resolution: case-insensitive,
    missing → 404 (None), ambiguous → 409, self-delegation → 409.
  * Cycle + depth guards on the parent chain.
  * Reply / error / cancellation injection paths produce the right
    structured prompt back into the parent session via
    `SessionManager.start_message`.
  * Multiple concurrent delegations under the same target agent.
  * Bridge broadcast scoping skips delegation children automatically
    (no chat ever maps to a delegation session id).
  * The REST routes (POST/GET/cancel under
    `/api/sessions/{sid}/delegations`).

We use real DelegationManager instances bound to per-test in-memory
SessionManagers. The parent session's `start_message` is patched in
the broadcast-injection tests so we never spawn a real harness — the
captured prompts are what matters.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from server.agent_manager import AgentManager
from server.database import Database
from server.delegations import (
    DEPTH_CAP,
    DelegationError,
    DelegationManager,
    delegation_manager as singleton_delegation_manager,
)
from server.main import app
from server.routers import agents as agents_mod
from server.routers import schedules as schedules_mod
from server.scheduler import ScheduleRunner
from server.session_manager import session_manager

TOKEN = "changeme"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def db():
    d = Database(":memory:")
    await d.initialize()
    yield d
    await d.close()


@pytest.fixture
async def mgr(db):
    """Per-test SessionManager bound to a per-test DB."""
    from server.session_manager import SessionManager

    m = SessionManager()
    await m.initialize(db)
    yield m


@pytest.fixture
async def dm(mgr, db):
    """Per-test DelegationManager bound to the test session manager.
    A fresh instance every test so registries don't leak across cases."""
    dm = DelegationManager()
    dm.bind(session_mgr=mgr, db=db)
    yield dm
    dm.shutdown()


async def _make_agent(db, name: str) -> dict:
    am = AgentManager(db)
    return await am.create_agent(name=name)


async def _make_session(
    mgr, agent_id: str, *, name: str = "S", origin: str = "user",
    parent_session_id: str | None = None,
):
    return await mgr.create_session(
        agent_id=agent_id,
        name=name,
        working_dir="/tmp",
        origin=origin,
        parent_session_id=parent_session_id,
    )


# ---------------------------------------------------------------------------
# Schema migration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_schema_has_new_columns(db):
    cols = {row[1] for row in await db._column_info("sessions")}
    assert "parent_session_id" in cols
    assert "delegation_request" in cols


@pytest.mark.asyncio
async def test_origin_delegation_round_trips(mgr, db):
    parent_agent = await db.get_system_agent()
    child_agent = await _make_agent(db, "Vera")
    parent = await _make_session(mgr, parent_agent["id"], name="parent")
    child = await mgr.create_session(
        agent_id=child_agent["id"],
        name="vera-child",
        working_dir="/tmp",
        origin="delegation",
        parent_session_id=parent.id,
        delegation_request="please review",
    )
    assert child.origin == "delegation"
    assert child.parent_session_id == parent.id
    assert child.delegation_request == "please review"

    # Persists across a reload from the DB.
    rows = await db.load_sessions()
    raw = next(r for r in rows if r["id"] == child.id)
    assert raw["origin"] == "delegation"
    assert raw["parent_session_id"] == parent.id
    assert raw["delegation_request"] == "please review"


@pytest.mark.asyncio
async def test_parent_delete_sets_null_on_child(mgr, db):
    parent_agent = await db.get_system_agent()
    child_agent = await _make_agent(db, "Vera")
    parent = await _make_session(mgr, parent_agent["id"])
    child = await mgr.create_session(
        agent_id=child_agent["id"],
        name="c",
        working_dir="/tmp",
        origin="delegation",
        parent_session_id=parent.id,
        delegation_request="r",
    )
    await db.delete_session(parent.id)
    rows = await db.load_sessions()
    raw = next(r for r in rows if r["id"] == child.id)
    assert raw["parent_session_id"] is None
    # The child itself survives — orphaning beats mass-delete.
    assert raw["id"] == child.id


# ---------------------------------------------------------------------------
# Agent resolution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_target_agent_case_insensitive(dm, db):
    await _make_agent(db, "Vera")
    assert (await dm._resolve_target_agent("vera"))["name"] == "Vera"
    assert (await dm._resolve_target_agent("VERA"))["name"] == "Vera"
    assert (await dm._resolve_target_agent("  Vera  "))["name"] == "Vera"


@pytest.mark.asyncio
async def test_resolve_target_agent_missing_returns_none(dm, db):
    assert await dm._resolve_target_agent("nobody") is None


# ---------------------------------------------------------------------------
# start_delegation: end-to-end happy path + guards
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_files_resolved_against_working_dir(
    dm, mgr, db, monkeypatch, tmp_path
):
    """Plan §5.7: file paths are resolved against the parent's
    working_dir and missing entries are flagged. Verifies the prompt
    composer no longer leaves relative paths verbatim (which made the
    'absolute under <working_dir>' claim a lie)."""
    started: list[tuple[str, str]] = []

    async def fake_start_message(sid, prompt, attachment_ids=None):
        started.append((sid, prompt))

    monkeypatch.setattr(mgr, "start_message", fake_start_message)
    # Build a real working dir with one file that exists and one
    # that doesn't, plus an absolute path to verify it passes through.
    wd = tmp_path / "ws"
    wd.mkdir()
    (wd / "real.tsx").write_text("hello")
    abs_real = tmp_path / "abs.txt"
    abs_real.write_text("there")

    octo = await db.get_system_agent()
    await _make_agent(db, "Vera")
    parent = await mgr.create_session(
        agent_id=octo["id"], name="parent", working_dir=str(wd),
    )
    await dm.start_delegation(
        parent_session_id=parent.id,
        agent_name="vera",
        request="review",
        files=["real.tsx", str(abs_real), "missing.tsx"],
    )
    assert started
    _, prompt = started[0]
    # Relative path resolved against working_dir.
    assert str((wd / "real.tsx").resolve()) in prompt
    # Absolute path passes through unchanged (modulo .resolve()).
    assert str(abs_real.resolve()) in prompt
    # Missing path is flagged so the child doesn't trust it.
    assert "(not found)" in prompt
    # Prompt text now reflects the resolution semantics.
    assert "paths resolved against" in prompt


@pytest.mark.asyncio
async def test_delegation_session_archived_after_terminal_inject(
    dm, mgr, db, monkeypatch
):
    """Plan §5.2 with Vera's round-2 nuance: a delegation child is
    archived when its OWN terminal turn has been injected into its
    parent — NOT on generic idle (which would prematurely archive an
    intermediate parent that's waiting for its own child to reply).
    Verifies _inject_terminal drives auto_archive_scheduled_session
    itself, so the chain work is genuinely complete by the time the
    archive runs."""
    monkeypatch.setattr(mgr, "start_message", _noop_start_message)
    octo = await db.get_system_agent()
    await _make_agent(db, "Vera")
    parent = await _make_session(mgr, octo["id"])
    rec = await dm.start_delegation(
        parent_session_id=parent.id, agent_name="vera", request="r",
    )
    child = mgr.get_session(rec.delegation_id)
    assert child is not None
    child._active_task = None
    # Drive the terminal event. _inject_terminal should both inject
    # into the parent AND archive the child.
    await dm._on_broadcast({
        "type": "result", "session_id": rec.delegation_id, "is_error": False,
    })
    assert rec.state == "completed"
    assert mgr.get_session(rec.delegation_id) is None


@pytest.mark.asyncio
async def test_idle_handler_does_not_archive_delegation_child(
    dm, mgr, db, monkeypatch
):
    """Vera's round-2 BLOCKING finding: the generic idle hook used to
    archive delegation children, breaking nested chains. The
    intermediate parent (Vera in Octo→Vera→Pete) is idle while
    waiting for its grandchild's reply; archiving it from the idle
    path would orphan the grandchild's terminal turn.

    _AUTO_ARCHIVE_ORIGINS no longer includes 'delegation' — only
    'schedule'. The auto_archive_scheduled_session helper itself
    still ACCEPTS delegation origin (it's how DelegationManager
    archives after terminal inject), but the idle path no longer
    calls it for delegation children."""
    monkeypatch.setattr(mgr, "start_message", _noop_start_message)
    octo = await db.get_system_agent()
    await _make_agent(db, "Vera")
    parent = await _make_session(mgr, octo["id"])
    rec = await dm.start_delegation(
        parent_session_id=parent.id, agent_name="vera", request="r",
    )
    child = mgr.get_session(rec.delegation_id)
    assert child is not None
    # The session is still alive; the auto-archive idle-origins set
    # does NOT include "delegation", so a generic idle wouldn't fire
    # the helper for it.
    assert "delegation" not in mgr._AUTO_ARCHIVE_ORIGINS
    # Confirm: even with the child quiesced, the auto-archive helper
    # archives it when called directly (the DelegationManager path),
    # but it's never called from the idle hook for a delegation child.
    child._active_task = None
    ok = await mgr.auto_archive_scheduled_session(rec.delegation_id)
    assert ok is True  # helper still accepts delegation origin
    assert mgr.get_session(rec.delegation_id) is None


@pytest.mark.asyncio
async def test_auto_archive_skips_user_origin_sessions(mgr, db, monkeypatch):
    """Sanity check: the auto-archive helper still respects origin —
    user-origin sessions must NOT be auto-archived even when the
    helper is called directly."""
    octo = await db.get_system_agent()
    sess = await _make_session(mgr, octo["id"], origin="user")
    sess._active_task = None
    ok = await mgr.auto_archive_scheduled_session(sess.id)
    assert ok is False
    assert mgr.get_session(sess.id) is not None


@pytest.mark.asyncio
async def test_orphaned_delegation_archived_on_restart(db):
    """A restart wipes DelegationManager._records (in-memory only) and
    kills the child's subprocess, so a delegation child that was live at
    restart can never finish or auto-archive itself — it would linger
    forever in the sidebar's hidden-delegation count. SessionManager's
    boot-time sweep archives such orphans; a healthy user-origin session
    is left untouched."""
    from server.session_manager import SessionManager

    # Boot 1: a normal parent + a live delegation child (mid-flight when
    # the process dies).
    m1 = SessionManager()
    await m1.initialize(db)
    octo = await db.get_system_agent()
    vera = await _make_agent(db, "Vera")
    parent = await _make_session(m1, octo["id"], name="parent")
    child = await m1.create_session(
        agent_id=vera["id"], name="vera-child", working_dir="/tmp",
        origin="delegation", parent_session_id=parent.id,
        delegation_request="r",
    )
    assert child.id in m1.sessions  # live before the restart

    # Boot 2: a fresh manager over the same DB — the run registry is gone.
    m2 = SessionManager()
    await m2.initialize(db)

    # The orphaned delegation child is swept into the archive and dropped
    # from memory; the user-origin parent survives as a live session.
    assert child.id not in m2.sessions
    assert parent.id in m2.sessions
    rows = {r["id"]: r for r in await db.load_sessions(include_archived=True)}
    assert rows[child.id]["archived"]
    assert not rows[parent.id]["archived"]


@pytest.mark.asyncio
async def test_nested_chain_intermediate_stays_alive_while_grandchild_runs(
    dm, mgr, db, monkeypatch
):
    """The full Vera scenario: Octo asks Vera, Vera asks Pete and
    ends her turn. Vera is now idle waiting for Pete. With the
    pre-fix behaviour, Vera would be archived on idle and Pete's
    reply would land on a missing session. With the fix, Vera stays
    alive until her OWN terminal event fires.

    Vera caught the round-2 version of this test using
    ``_noop_start_message``, which silently absorbed missing-parent
    injections and therefore couldn't observe the original bug. This
    revision uses a fake that mirrors the real ``start_message``'s
    failure mode: it raises when the target session id is no longer
    in the in-memory map. So if anything regresses to archiving Vera
    while Pete is still running, Pete's `_inject_terminal` will fail
    the assertion via the captured exception list, not silently
    succeed.
    """
    octo = await db.get_system_agent()
    await _make_agent(db, "Vera")
    await _make_agent(db, "Pete")
    octo_sess = await _make_session(mgr, octo["id"], name="octo-user")

    from server import delegations as _delegations_module

    delivered: list[tuple[str, str]] = []
    inject_errors: list[str] = []

    async def faithful_start_message(sid, prompt, attachment_ids=None):
        # Mirror SessionManager.start_message: ValueError when the
        # target session id is no longer in the in-memory map. Any
        # archival-too-early bug surfaces here as an exception that
        # the DelegationManager's logger absorbs — we capture it via
        # monkeypatching logger.exception below so the test fails
        # rather than passing under the bug.
        if sid not in mgr.sessions:
            raise ValueError(f"Session {sid} not found")
        delivered.append((sid, prompt))

    def capture_logger_exception(msg, *args, **kwargs):
        # When DelegationManager._inject_terminal catches a missing-
        # parent error, it logs via logger.exception. We capture that
        # so a regression to the original bug becomes a hard failure.
        try:
            inject_errors.append(msg % args if args else msg)
        except Exception:
            inject_errors.append(str(msg))

    monkeypatch.setattr(mgr, "start_message", faithful_start_message)
    monkeypatch.setattr(
        _delegations_module.logger,
        "exception",
        capture_logger_exception,
    )

    # Octo → Vera.
    vera_rec = await dm.start_delegation(
        parent_session_id=octo_sess.id, agent_name="vera", request="r1",
    )
    vera_sess = mgr.get_session(vera_rec.delegation_id)
    assert vera_sess is not None
    # Vera → Pete.
    pete_rec = await dm.start_delegation(
        parent_session_id=vera_sess.id, agent_name="pete", request="r2",
    )
    # Vera is now waiting for Pete; she has no active work of her own.
    vera_sess._active_task = None

    # Drive session_manager's actual idle hook for Vera (mirror of
    # _consume_message's post-queue-drain block). With the round-2
    # fix in place, "delegation" is NOT in _AUTO_ARCHIVE_ORIGINS so
    # this is a no-op; if a future change re-adds it (the original
    # bug), Vera gets archived here and Pete's reply below fails.
    # Vera flagged that the test relied on an adjacent test to catch
    # this directly; this in-place hook makes the nested-chain test
    # self-sufficient at proving the whole bug.
    if vera_sess.origin in mgr._AUTO_ARCHIVE_ORIGINS:
        await mgr.auto_archive_scheduled_session(vera_sess.id)

    # Drive Pete to terminal. If Vera was archived too early,
    # faithful_start_message raises and capture_logger_exception
    # records it — the assertions below fail.
    await dm._on_broadcast({
        "type": "result",
        "session_id": pete_rec.delegation_id,
        "is_error": False,
    })
    assert not inject_errors, (
        f"Pete's reply failed to reach Vera — Vera was archived "
        f"too early. Captured errors: {inject_errors!r}"
    )
    # Pete's reply actually landed on Vera's session id.
    assert any(sid == vera_rec.delegation_id for sid, _ in delivered), (
        f"Pete's terminal injection didn't target Vera. "
        f"delivered={delivered!r}"
    )
    # Pete's session is archived now that his chain work is done.
    assert mgr.get_session(pete_rec.delegation_id) is None
    # Vera is STILL alive — she hasn't fired her own terminal yet.
    assert mgr.get_session(vera_rec.delegation_id) is not None

    # Now Vera fires her own terminal. Pete-style.
    await dm._on_broadcast({
        "type": "result",
        "session_id": vera_rec.delegation_id,
        "is_error": False,
    })
    assert not inject_errors, (
        f"Vera's reply failed to reach Octo. Captured errors: "
        f"{inject_errors!r}"
    )
    # Vera is archived now. Octo (root user session) is untouched.
    assert mgr.get_session(vera_rec.delegation_id) is None
    assert mgr.get_session(octo_sess.id) is octo_sess


@pytest.mark.asyncio
async def test_start_delegation_happy_path(dm, mgr, db, monkeypatch):
    # Disable real harness spawn — we only want to verify the wiring.
    started: list[tuple[str, str]] = []

    async def fake_start_message(sid, prompt, attachment_ids=None):
        started.append((sid, prompt))

    monkeypatch.setattr(mgr, "start_message", fake_start_message)

    parent_agent = await db.get_system_agent()
    await _make_agent(db, "Vera")
    parent = await _make_session(mgr, parent_agent["id"], name="parent")

    rec = await dm.start_delegation(
        parent_session_id=parent.id,
        agent_name="vera",
        request="review the dashboard",
        files=["web/src/Dashboard.tsx"],
    )
    assert rec.state == "running"
    assert rec.delegation_id != parent.id
    assert rec.target_agent_name == "Vera"
    assert rec.parent_session_id == parent.id

    # A child session row exists with origin=delegation, parent set.
    child = mgr.get_session(rec.delegation_id)
    assert child is not None
    assert child.origin == "delegation"
    assert child.parent_session_id == parent.id
    assert child.delegation_request == "review the dashboard"
    assert child.working_dir == parent.working_dir  # inherited

    # The child's first turn was kicked off — the composed prompt
    # mentions the parent agent and the request, and lists files.
    assert started, "expected start_message to be called on the child"
    sid, prompt = started[0]
    assert sid == rec.delegation_id
    assert "Octo" in prompt  # default system agent's name
    assert "review the dashboard" in prompt
    assert "web/src/Dashboard.tsx" in prompt


@pytest.mark.asyncio
async def test_start_delegation_rejects_empty_request(dm, mgr, db, monkeypatch):
    monkeypatch.setattr(mgr, "start_message", _noop_start_message)
    await _make_agent(db, "Vera")
    parent_agent = await db.get_system_agent()
    parent = await _make_session(mgr, parent_agent["id"])
    with pytest.raises(DelegationError) as excinfo:
        await dm.start_delegation(
            parent_session_id=parent.id,
            agent_name="vera",
            request="   ",
        )
    assert excinfo.value.status_code == 400


@pytest.mark.asyncio
async def test_start_delegation_unknown_parent(dm):
    with pytest.raises(DelegationError) as excinfo:
        await dm.start_delegation(
            parent_session_id="nonexistent",
            agent_name="vera",
            request="hi",
        )
    assert excinfo.value.status_code == 404


@pytest.mark.asyncio
async def test_start_delegation_unknown_target(dm, mgr, db, monkeypatch):
    monkeypatch.setattr(mgr, "start_message", _noop_start_message)
    parent_agent = await db.get_system_agent()
    parent = await _make_session(mgr, parent_agent["id"])
    with pytest.raises(DelegationError) as excinfo:
        await dm.start_delegation(
            parent_session_id=parent.id,
            agent_name="ghost",
            request="hi",
        )
    assert excinfo.value.status_code == 404


@pytest.mark.asyncio
async def test_start_delegation_rejects_self(dm, mgr, db, monkeypatch):
    monkeypatch.setattr(mgr, "start_message", _noop_start_message)
    parent_agent = await db.get_system_agent()
    parent = await _make_session(mgr, parent_agent["id"])
    with pytest.raises(DelegationError) as excinfo:
        await dm.start_delegation(
            parent_session_id=parent.id,
            agent_name=parent_agent["name"],
            request="hi",
        )
    assert excinfo.value.status_code == 409
    assert "yourself" in str(excinfo.value)


@pytest.mark.asyncio
async def test_cycle_rejected(dm, mgr, db, monkeypatch):
    """Octo → Vera → Octo is rejected at the cycle check."""
    monkeypatch.setattr(mgr, "start_message", _noop_start_message)
    octo = await db.get_system_agent()
    vera = await _make_agent(db, "Vera")

    # Octo's user session
    octo_sess = await _make_session(mgr, octo["id"], name="octo-user")
    # Vera child under Octo
    vera_sess = await mgr.create_session(
        agent_id=vera["id"],
        name="vera",
        working_dir="/tmp",
        origin="delegation",
        parent_session_id=octo_sess.id,
        delegation_request="r",
    )
    # Now Vera tries to ask Octo — cycle.
    with pytest.raises(DelegationError) as excinfo:
        await dm.start_delegation(
            parent_session_id=vera_sess.id,
            agent_name=octo["name"],
            request="back to you",
        )
    assert excinfo.value.status_code == 409
    assert "Cycle" in str(excinfo.value)


@pytest.mark.asyncio
async def test_chain_walk_rejects_session_id_cycle(dm, mgr, db, monkeypatch):
    """If the parent_session_id pointers form a session-id cycle
    (corrupted, not a real agent cycle), fail closed rather than
    silently treating the chain as terminated. The cycle is forced
    in memory after a valid creation, so the SQLite FK on
    parent_session_id (which would catch the obvious construction
    error) doesn't apply."""
    monkeypatch.setattr(mgr, "start_message", _noop_start_message)
    octo = await db.get_system_agent()
    await _make_agent(db, "Vera")
    a = await _make_agent(db, "A")
    # Build a valid 2-session chain first so the FK is satisfied.
    sess_a = await _make_session(mgr, octo["id"], name="a")
    sess_b = await mgr.create_session(
        agent_id=a["id"], name="b", working_dir="/tmp",
        origin="delegation", parent_session_id=sess_a.id,
        delegation_request="r",
    )
    # Force the cycle in memory only — corrupting the in-memory
    # pointer chain without touching the DB. _check_chain walks via
    # mgr.get_session which reads the in-memory map.
    sess_a.parent_session_id = sess_b.id
    with pytest.raises(DelegationError) as ex:
        await dm.start_delegation(
            parent_session_id=sess_a.id, agent_name="vera", request="r",
        )
    assert ex.value.status_code == 409
    assert "session-id cycle" in str(ex.value)


@pytest.mark.asyncio
async def test_chain_walk_falls_back_to_db_for_archived_ancestor(
    dm, mgr, db, monkeypatch
):
    """Vera's round-3 finding: after the auto-archive-after-terminal
    fix, a delegation child can legitimately have its parent archived
    in the DB. If the user then unarchives the child, ``ask_agent``
    from it must still succeed — the walk consults the DB for any
    ancestor missing from the in-memory map. Without this fallback,
    every fresh delegation from an unarchived child would 409 with
    "no longer exists"."""
    monkeypatch.setattr(mgr, "start_message", _noop_start_message)
    octo = await db.get_system_agent()
    vera = await _make_agent(db, "Vera")
    pete = await _make_agent(db, "Pete")
    # Build a valid chain Octo (root) → Vera-child (delegation).
    octo_sess = await _make_session(mgr, octo["id"], name="octo")
    vera_child = await mgr.create_session(
        agent_id=vera["id"], name="vera-child", working_dir="/tmp",
        origin="delegation", parent_session_id=octo_sess.id,
        delegation_request="r",
    )
    # Evict octo_sess from the in-memory map as if it had been
    # archived. The DB row is still there.
    mgr.sessions.pop(octo_sess.id, None)
    # ask_agent from Vera-child to Pete should still work — the DB
    # fallback finds the archived ancestor and the walk completes
    # cleanly. With Vera + Pete + Octo's archived agent in the
    # chain, we still satisfy the depth+cycle checks.
    rec = await dm.start_delegation(
        parent_session_id=vera_child.id, agent_name="pete", request="r",
    )
    assert rec.state == "running"


@pytest.mark.asyncio
async def test_chain_walk_rejects_truly_missing_ancestor(
    dm, mgr, db, monkeypatch
):
    """Only fail-closed when the ancestor is gone from BOTH memory
    AND the DB. A stale parent_session_id pointing at a hard-deleted
    row still produces a 409 — that's actual corruption, not a
    legitimate archived state."""
    monkeypatch.setattr(mgr, "start_message", _noop_start_message)
    octo = await db.get_system_agent()
    await _make_agent(db, "Vera")
    ancestor = await _make_session(mgr, octo["id"], name="ancestor")
    child = await mgr.create_session(
        agent_id=octo["id"], name="orphan", working_dir="/tmp",
        origin="delegation", parent_session_id=ancestor.id,
        delegation_request="r",
    )
    # Evict from memory AND from the DB so the fallback also misses.
    mgr.sessions.pop(ancestor.id, None)
    await db.delete_session(ancestor.id)
    with pytest.raises(DelegationError) as ex:
        await dm.start_delegation(
            parent_session_id=child.id, agent_name="vera", request="r",
        )
    assert ex.value.status_code == 409
    assert "neither memory nor the database" in str(ex.value)


@pytest.mark.asyncio
async def test_depth_cap_rejected(dm, mgr, db, monkeypatch):
    """A 4th delegation hop is rejected (DEPTH_CAP=3)."""
    monkeypatch.setattr(mgr, "start_message", _noop_start_message)
    octo = await db.get_system_agent()
    a = await _make_agent(db, "A")
    b = await _make_agent(db, "B")
    c = await _make_agent(db, "C")
    d = await _make_agent(db, "D")

    root = await _make_session(mgr, octo["id"], name="root")
    s_a = await mgr.create_session(
        agent_id=a["id"], name="a", working_dir="/tmp",
        origin="delegation", parent_session_id=root.id, delegation_request="r",
    )
    s_b = await mgr.create_session(
        agent_id=b["id"], name="b", working_dir="/tmp",
        origin="delegation", parent_session_id=s_a.id, delegation_request="r",
    )
    s_c = await mgr.create_session(
        agent_id=c["id"], name="c", working_dir="/tmp",
        origin="delegation", parent_session_id=s_b.id, delegation_request="r",
    )
    # Chain so far: 3 delegation hops (A, B, C). Adding D would make 4.
    with pytest.raises(DelegationError) as excinfo:
        await dm.start_delegation(
            parent_session_id=s_c.id, agent_name=d["name"], request="r",
        )
    assert excinfo.value.status_code == 409
    assert str(DEPTH_CAP) in str(excinfo.value)


# ---------------------------------------------------------------------------
# Broadcast → injection
# ---------------------------------------------------------------------------


async def _noop_start_message(sid, prompt, attachment_ids=None):
    return None


@pytest.mark.asyncio
async def test_reply_injection_on_result(dm, mgr, db, monkeypatch):
    """assistant_text + result(is_error=False) → [agent-reply:...] turn."""
    injected: list[tuple[str, str]] = []

    async def capture(sid, prompt, attachment_ids=None):
        injected.append((sid, prompt))

    monkeypatch.setattr(mgr, "start_message", capture)
    octo = await db.get_system_agent()
    await _make_agent(db, "Vera")
    parent = await _make_session(mgr, octo["id"], name="parent")
    rec = await dm.start_delegation(
        parent_session_id=parent.id, agent_name="vera", request="r",
    )
    # First call was the child's start_message; clear it so we only
    # see the injection.
    injected.clear()

    cid = rec.delegation_id
    await dm._on_broadcast({"type": "assistant_text", "session_id": cid, "content": "Reviewed. "})
    await dm._on_broadcast({"type": "assistant_text", "session_id": cid, "content": "Looks good."})
    await dm._on_broadcast({"type": "result", "session_id": cid, "is_error": False})

    assert rec.state == "completed"
    assert rec.finished_at is not None
    assert len(injected) == 1
    target_sid, prompt = injected[0]
    assert target_sid == parent.id
    assert prompt.startswith(f"[agent-reply:Vera delegation={cid}]")
    assert "Reviewed. Looks good." in prompt


@pytest.mark.asyncio
async def test_error_injection_on_result_error(dm, mgr, db, monkeypatch):
    """result(is_error=True) → [agent-error:...] turn, state=failed."""
    injected: list[tuple[str, str]] = []

    async def capture(sid, prompt, attachment_ids=None):
        injected.append((sid, prompt))

    monkeypatch.setattr(mgr, "start_message", capture)
    octo = await db.get_system_agent()
    await _make_agent(db, "Vera")
    parent = await _make_session(mgr, octo["id"])
    rec = await dm.start_delegation(
        parent_session_id=parent.id, agent_name="vera", request="r",
    )
    injected.clear()

    cid = rec.delegation_id
    await dm._on_broadcast({"type": "result", "session_id": cid, "is_error": True})

    assert rec.state == "failed"
    assert len(injected) == 1
    _, prompt = injected[0]
    assert prompt.startswith(f"[agent-error:Vera delegation={cid}")


@pytest.mark.asyncio
async def test_error_injection_on_error_event(dm, mgr, db, monkeypatch):
    """An explicit error event from the child also terminates."""
    injected: list[tuple[str, str]] = []

    async def capture(sid, prompt, attachment_ids=None):
        injected.append((sid, prompt))

    monkeypatch.setattr(mgr, "start_message", capture)
    octo = await db.get_system_agent()
    await _make_agent(db, "Vera")
    parent = await _make_session(mgr, octo["id"])
    rec = await dm.start_delegation(
        parent_session_id=parent.id, agent_name="vera", request="r",
    )
    injected.clear()

    cid = rec.delegation_id
    await dm._on_broadcast({
        "type": "error", "session_id": cid, "message": "harness died",
    })

    assert rec.state == "failed"
    assert "harness died" in rec.error
    assert len(injected) == 1


@pytest.mark.asyncio
async def test_post_terminal_events_ignored(dm, mgr, db, monkeypatch):
    """Once a delegation is terminal, late events don't reopen it."""
    monkeypatch.setattr(mgr, "start_message", _noop_start_message)
    octo = await db.get_system_agent()
    await _make_agent(db, "Vera")
    parent = await _make_session(mgr, octo["id"])
    rec = await dm.start_delegation(
        parent_session_id=parent.id, agent_name="vera", request="r",
    )
    cid = rec.delegation_id
    await dm._on_broadcast({"type": "result", "session_id": cid, "is_error": False})
    state_before = rec.state
    await dm._on_broadcast({"type": "assistant_text", "session_id": cid, "content": "late"})
    await dm._on_broadcast({"type": "result", "session_id": cid, "is_error": True})
    assert rec.state == state_before


@pytest.mark.asyncio
async def test_empty_reply_gets_placeholder(dm, mgr, db, monkeypatch):
    """A child that ends successfully without any assistant_text still
    gets a reply injection, with a placeholder body."""
    injected: list[tuple[str, str]] = []

    async def capture(sid, prompt, attachment_ids=None):
        injected.append((sid, prompt))

    monkeypatch.setattr(mgr, "start_message", capture)
    octo = await db.get_system_agent()
    await _make_agent(db, "Vera")
    parent = await _make_session(mgr, octo["id"])
    rec = await dm.start_delegation(
        parent_session_id=parent.id, agent_name="vera", request="r",
    )
    injected.clear()
    cid = rec.delegation_id
    await dm._on_broadcast({"type": "result", "session_id": cid, "is_error": False})
    _, prompt = injected[0]
    assert "without producing any text" in prompt


# ---------------------------------------------------------------------------
# Cancellation, listing, concurrency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_delegation(dm, mgr, db, monkeypatch):
    injected: list[tuple[str, str]] = []

    async def capture(sid, prompt, attachment_ids=None):
        injected.append((sid, prompt))

    async def fake_interrupt(sid):
        return True

    monkeypatch.setattr(mgr, "start_message", capture)
    monkeypatch.setattr(mgr, "interrupt", fake_interrupt)
    octo = await db.get_system_agent()
    await _make_agent(db, "Vera")
    parent = await _make_session(mgr, octo["id"])
    rec = await dm.start_delegation(
        parent_session_id=parent.id, agent_name="vera", request="r",
    )
    injected.clear()
    updated = await dm.cancel_delegation(rec.delegation_id, reason="user clicked stop")
    assert updated.state == "cancelled"
    assert len(injected) == 1
    _, prompt = injected[0]
    assert "agent-error:Vera" in prompt
    assert "user clicked stop" in prompt
    # Idempotent.
    again = await dm.cancel_delegation(rec.delegation_id)
    assert again.state == "cancelled"


# ---------------------------------------------------------------------------
# follow_up_delegation: continue a prior delegation in the same child session
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_follow_up_happy_path(dm, mgr, db, monkeypatch):
    """Octo asks Vera, Vera replies + her session auto-archives. Octo
    follows up with a new round in the same session. The child is
    unarchived, the record is reset to running, and the new request
    flows to start_message — Vera will see her previous round in
    her transcript when she resumes."""
    injected: list[tuple[str, str]] = []

    async def capture(sid, prompt, attachment_ids=None):
        injected.append((sid, prompt))

    monkeypatch.setattr(mgr, "start_message", capture)
    octo = await db.get_system_agent()
    await _make_agent(db, "Vera")
    parent = await _make_session(mgr, octo["id"], name="octo")
    rec = await dm.start_delegation(
        parent_session_id=parent.id, agent_name="vera", request="round 1",
    )
    # Drive Vera to terminal so she auto-archives.
    await dm._on_broadcast({
        "type": "result", "session_id": rec.delegation_id, "is_error": False,
    })
    assert rec.state == "completed"
    assert mgr.get_session(rec.delegation_id) is None  # archived

    # Follow up.
    injected.clear()
    updated = await dm.follow_up_delegation(
        parent_session_id=parent.id,
        delegation_id=rec.delegation_id,
        request="round 2 — please re-check finding 3",
    )
    # Same identity, fresh round.
    assert updated.delegation_id == rec.delegation_id
    assert updated.state == "running"
    assert updated.request == "round 2 — please re-check finding 3"
    assert updated.captured_text == []
    assert updated.error is None
    assert updated.finished_at is None
    assert updated._terminal_injected is False
    # Child is back in the live map.
    assert mgr.get_session(rec.delegation_id) is not None
    # The new request was delivered to the child's session.
    assert injected
    sid, prompt = injected[0]
    assert sid == rec.delegation_id
    assert "follow-up" in prompt.lower()
    assert "round 2 — please re-check finding 3" in prompt
    # The full chain still works: Vera's NEW reply lands on Octo.
    injected.clear()
    await dm._on_broadcast({
        "type": "assistant_text",
        "session_id": rec.delegation_id,
        "content": "Re-checked. Looks fixed.",
    })
    await dm._on_broadcast({
        "type": "result", "session_id": rec.delegation_id, "is_error": False,
    })
    assert updated.state == "completed"
    octo_injects = [p for s, p in injected if s == parent.id]
    assert len(octo_injects) == 1
    assert "Re-checked. Looks fixed." in octo_injects[0]


@pytest.mark.asyncio
async def test_follow_up_rejects_while_running(dm, mgr, db, monkeypatch):
    monkeypatch.setattr(mgr, "start_message", _noop_start_message)
    octo = await db.get_system_agent()
    await _make_agent(db, "Vera")
    parent = await _make_session(mgr, octo["id"])
    rec = await dm.start_delegation(
        parent_session_id=parent.id, agent_name="vera", request="r",
    )
    assert rec.state == "running"
    with pytest.raises(DelegationError) as ex:
        await dm.follow_up_delegation(
            parent_session_id=parent.id,
            delegation_id=rec.delegation_id,
            request="round 2",
        )
    assert ex.value.status_code == 409
    assert "still running" in str(ex.value)


@pytest.mark.asyncio
async def test_follow_up_rejects_unknown_delegation(dm, mgr, db, monkeypatch):
    monkeypatch.setattr(mgr, "start_message", _noop_start_message)
    octo = await db.get_system_agent()
    parent = await _make_session(mgr, octo["id"])
    with pytest.raises(DelegationError) as ex:
        await dm.follow_up_delegation(
            parent_session_id=parent.id,
            delegation_id="ghost",
            request="hi",
        )
    assert ex.value.status_code == 404


@pytest.mark.asyncio
async def test_follow_up_rejects_wrong_parent(dm, mgr, db, monkeypatch):
    """A delegation can only be followed up by its OWN parent — a
    different session can't continue someone else's conversation."""
    monkeypatch.setattr(mgr, "start_message", _noop_start_message)
    octo = await db.get_system_agent()
    await _make_agent(db, "Vera")
    parent_a = await _make_session(mgr, octo["id"], name="A")
    parent_b = await _make_session(mgr, octo["id"], name="B")
    rec = await dm.start_delegation(
        parent_session_id=parent_a.id, agent_name="vera", request="r",
    )
    await dm._on_broadcast({
        "type": "result", "session_id": rec.delegation_id, "is_error": False,
    })
    with pytest.raises(DelegationError) as ex:
        await dm.follow_up_delegation(
            parent_session_id=parent_b.id,
            delegation_id=rec.delegation_id,
            request="hi",
        )
    assert ex.value.status_code == 404


@pytest.mark.asyncio
async def test_follow_up_rejects_empty_request(dm, mgr, db, monkeypatch):
    monkeypatch.setattr(mgr, "start_message", _noop_start_message)
    octo = await db.get_system_agent()
    await _make_agent(db, "Vera")
    parent = await _make_session(mgr, octo["id"])
    rec = await dm.start_delegation(
        parent_session_id=parent.id, agent_name="vera", request="r",
    )
    await dm._on_broadcast({
        "type": "result", "session_id": rec.delegation_id, "is_error": False,
    })
    with pytest.raises(DelegationError) as ex:
        await dm.follow_up_delegation(
            parent_session_id=parent.id,
            delegation_id=rec.delegation_id,
            request="   ",
        )
    assert ex.value.status_code == 400


@pytest.mark.asyncio
async def test_route_follow_up_requires_live_parent(client, monkeypatch):
    """Vera-round-5 finding: a follow-up from a parent session that's
    no longer in the live sessions map (archived / deleted between
    rounds) must 404 — otherwise the manager would round-reset the
    record and start the child, then silently drop the terminal
    turn because there's no live parent to inject into."""
    async def noop(sid, prompt, attachment_ids=None):
        return None

    monkeypatch.setattr(session_manager, "start_message", noop)

    agents = (await client.get("/api/agents", headers=HEADERS)).json()
    octo = next(a for a in agents if a["is_system"])
    await _post_agent(client, "Vera")
    parent = await _post_session(client, octo["id"])
    create = await client.post(
        f"/api/sessions/{parent['id']}/delegations",
        json={"agent_name": "vera", "request": "round 1"},
        headers=HEADERS,
    )
    did = create.json()["delegation_id"]
    await singleton_delegation_manager._on_broadcast({
        "type": "result", "session_id": did, "is_error": False,
    })
    # Evict the parent session from the live map (simulating archive
    # / hard delete between rounds).
    session_manager.sessions.pop(parent["id"], None)

    r = await client.post(
        f"/api/sessions/{parent['id']}/delegations/{did}/follow-up",
        json={"request": "round 2"},
        headers=HEADERS,
    )
    assert r.status_code == 404
    # The record must still be in its terminal state — the route
    # rejected before round-reset, so subsequent attempts (if the
    # user unarchives the parent) still see the original state.
    rec = singleton_delegation_manager.get_delegation(did)
    assert rec is not None
    assert rec.state == "completed"


@pytest.mark.asyncio
async def test_route_follow_up(client, monkeypatch):
    """HTTP path mirrors the manager: 200/201 happy path; 404 for
    unknown delegation; 409 while running."""
    async def noop(sid, prompt, attachment_ids=None):
        return None

    monkeypatch.setattr(session_manager, "start_message", noop)

    agents = (await client.get("/api/agents", headers=HEADERS)).json()
    octo = next(a for a in agents if a["is_system"])
    await _post_agent(client, "Vera")
    parent = await _post_session(client, octo["id"])
    create = await client.post(
        f"/api/sessions/{parent['id']}/delegations",
        json={"agent_name": "vera", "request": "round 1"},
        headers=HEADERS,
    )
    did = create.json()["delegation_id"]

    # Drive the delegation to terminal via the broadcast bus so the
    # record can be followed-up.
    await singleton_delegation_manager._on_broadcast({
        "type": "result", "session_id": did, "is_error": False,
    })

    r = await client.post(
        f"/api/sessions/{parent['id']}/delegations/{did}/follow-up",
        json={"request": "round 2"},
        headers=HEADERS,
    )
    assert r.status_code in (200, 201), r.text
    body = r.json()
    assert body["delegation_id"] == did
    assert body["state"] == "running"
    assert body["request"] == "round 2"


@pytest.mark.asyncio
async def test_cancel_cascades_to_descendants(dm, mgr, db, monkeypatch):
    """Vera's round-3 finding: cancelling a delegation must cascade
    to its descendants, otherwise a grandchild keeps burning tokens
    and its eventual reply lands on a cancelled parent.

    Scenario: Octo→Vera→Pete. Cancel Vera. Pete should also be
    cancelled with a reason naming the parent cancel."""
    injected: list[tuple[str, str]] = []

    async def capture(sid, prompt, attachment_ids=None):
        injected.append((sid, prompt))

    async def fake_interrupt(sid):
        return True

    monkeypatch.setattr(mgr, "start_message", capture)
    monkeypatch.setattr(mgr, "interrupt", fake_interrupt)
    octo = await db.get_system_agent()
    await _make_agent(db, "Vera")
    await _make_agent(db, "Pete")
    octo_sess = await _make_session(mgr, octo["id"], name="octo")
    vera_rec = await dm.start_delegation(
        parent_session_id=octo_sess.id, agent_name="vera", request="r1",
    )
    pete_rec = await dm.start_delegation(
        parent_session_id=vera_rec.delegation_id,
        agent_name="pete",
        request="r2",
    )
    assert pete_rec.state == "running"
    injected.clear()

    await dm.cancel_delegation(
        vera_rec.delegation_id, reason="user stop"
    )

    assert dm.get_delegation(vera_rec.delegation_id).state == "cancelled"
    # Pete cascade-cancelled too.
    assert dm.get_delegation(pete_rec.delegation_id).state == "cancelled"
    assert "parent delegation cancelled" in (
        dm.get_delegation(pete_rec.delegation_id).error or ""
    )
    # Two terminal injections: one into Vera's session for Pete's
    # cancel, one into Octo's session for Vera's cancel. Pete's
    # injection fires BEFORE Vera's so the cascade unwinds bottom-up.
    pete_injects = [p for s, p in injected if s == vera_rec.delegation_id]
    vera_injects = [p for s, p in injected if s == octo_sess.id]
    assert len(pete_injects) == 1
    assert "agent-error:Pete" in pete_injects[0]
    assert "user stop" in pete_injects[0]
    assert len(vera_injects) == 1
    assert "agent-error:Vera" in vera_injects[0]
    assert "user stop" in vera_injects[0]


@pytest.mark.asyncio
async def test_cancel_delegation_single_inject_under_interrupt_broadcast(
    dm, mgr, db, monkeypatch
):
    """The bug Vera caught: cancel_delegation() calls interrupt()
    which broadcasts an `error` event before returning; without the
    state-flip-first dance, `_on_broadcast` would catch that error,
    finalize the record as `failed`, and inject `[agent-error
    reason=child error]`. Then cancel_delegation would inject a
    SECOND `[agent-error reason=cancelled]` — two terminal turns
    for one cancellation.

    With the fix, state flips to "cancelled" before interrupt fires,
    so `_on_broadcast` short-circuits on `rec.state != "running"`."""
    injected: list[tuple[str, str]] = []

    async def capture(sid, prompt, attachment_ids=None):
        injected.append((sid, prompt))

    monkeypatch.setattr(mgr, "start_message", capture)

    async def fake_interrupt(sid):
        # Mirror the real session_manager.interrupt: broadcast an
        # `error` event on the child session before returning.
        await dm._on_broadcast({
            "type": "error",
            "session_id": sid,
            "message": "(interrupted by user)",
        })
        return True

    monkeypatch.setattr(mgr, "interrupt", fake_interrupt)
    octo = await db.get_system_agent()
    await _make_agent(db, "Vera")
    parent = await _make_session(mgr, octo["id"])
    rec = await dm.start_delegation(
        parent_session_id=parent.id, agent_name="vera", request="r",
    )
    injected.clear()
    updated = await dm.cancel_delegation(
        rec.delegation_id, reason="user stop"
    )
    assert updated.state == "cancelled"
    # Single terminal injection, NOT two.
    assert len(injected) == 1, (
        f"expected one terminal turn for one cancellation, "
        f"got {len(injected)}: {injected!r}"
    )
    _, prompt = injected[0]
    # The reason should be the cancel reason, not the "child error"
    # placeholder — confirms the cancel path won, not the error path.
    assert "user stop" in prompt
    assert "child session error" not in prompt


@pytest.mark.asyncio
async def test_terminal_injection_is_idempotent(dm, mgr, db, monkeypatch):
    """Belt-and-suspenders: even if two terminal-producing events
    race past the state guard (e.g. a `result` and an `error` from a
    crashing child arrive in quick succession), exactly one
    `[agent-…]` turn lands in the parent."""
    injected: list[tuple[str, str]] = []

    async def capture(sid, prompt, attachment_ids=None):
        injected.append((sid, prompt))

    monkeypatch.setattr(mgr, "start_message", capture)
    octo = await db.get_system_agent()
    await _make_agent(db, "Vera")
    parent = await _make_session(mgr, octo["id"])
    rec = await dm.start_delegation(
        parent_session_id=parent.id, agent_name="vera", request="r",
    )
    injected.clear()
    # Force the bypass: call `_inject_terminal` twice directly.
    rec.state = "completed"
    await dm._inject_terminal(rec)
    await dm._inject_terminal(rec)
    assert len(injected) == 1


@pytest.mark.asyncio
async def test_list_delegations_newest_first(dm, mgr, db, monkeypatch):
    monkeypatch.setattr(mgr, "start_message", _noop_start_message)
    octo = await db.get_system_agent()
    await _make_agent(db, "Vera")
    await _make_agent(db, "Pete")
    parent = await _make_session(mgr, octo["id"])
    r1 = await dm.start_delegation(
        parent_session_id=parent.id, agent_name="vera", request="r1",
    )
    r2 = await dm.start_delegation(
        parent_session_id=parent.id, agent_name="pete", request="r2",
    )
    listed = dm.list_delegations(parent.id)
    assert [r.delegation_id for r in listed] == [r2.delegation_id, r1.delegation_id]


@pytest.mark.asyncio
async def test_concurrent_delegations_to_same_target(dm, mgr, db, monkeypatch):
    """Two delegations to Vera in flight at once: both run, both reply
    independently, parent sees both terminal turns."""
    injected: list[tuple[str, str]] = []

    async def capture(sid, prompt, attachment_ids=None):
        injected.append((sid, prompt))

    monkeypatch.setattr(mgr, "start_message", capture)
    octo = await db.get_system_agent()
    await _make_agent(db, "Vera")
    parent = await _make_session(mgr, octo["id"])

    rec1 = await dm.start_delegation(
        parent_session_id=parent.id, agent_name="vera", request="r1",
    )
    rec2 = await dm.start_delegation(
        parent_session_id=parent.id, agent_name="vera", request="r2",
    )
    assert rec1.delegation_id != rec2.delegation_id
    injected.clear()

    # Send each child its own result.
    await dm._on_broadcast({"type": "assistant_text", "session_id": rec1.delegation_id, "content": "one"})
    await dm._on_broadcast({"type": "result", "session_id": rec1.delegation_id, "is_error": False})
    await dm._on_broadcast({"type": "assistant_text", "session_id": rec2.delegation_id, "content": "two"})
    await dm._on_broadcast({"type": "result", "session_id": rec2.delegation_id, "is_error": False})

    assert rec1.state == "completed"
    assert rec2.state == "completed"
    # Two terminal injections into the same parent, ordered by completion.
    assert len(injected) == 2
    assert all(t[0] == parent.id for t in injected)
    assert f"delegation={rec1.delegation_id}" in injected[0][1]
    assert f"delegation={rec2.delegation_id}" in injected[1][1]


# ---------------------------------------------------------------------------
# Bridges don't fan out delegation sessions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bridge_manager_skips_delegation_session(mgr, db, monkeypatch):
    """The bridge broadcast filter is session-id-scoped: a chat is bound
    to one sticky session. A delegation session has no bridge binding,
    so events from it never reach any chat. We verify by sending a
    broadcast for a delegation session id and confirming no bridge
    bridge.handle_event call was attempted."""
    from server.bridges.manager import BridgeManager

    bm = BridgeManager(mgr, db)

    # A chat bound to Vera but to the user-origin session of Vera, NOT
    # to a delegation child of Vera.
    octo = await db.get_system_agent()
    vera = await _make_agent(db, "Vera")
    vera_user_sess = await _make_session(mgr, vera["id"], name="vera-user")
    # Hand-register a binding to that user session id.
    from server.bridges.manager import ChatBinding

    bm._mappings["telegram:42"] = ChatBinding(
        agent_id=vera["id"], session_id=vera_user_sess.id, verbose=False
    )

    # Create a delegation child under Vera (parent = an Octo session).
    octo_sess = await _make_session(mgr, octo["id"])
    delegation_child = await mgr.create_session(
        agent_id=vera["id"], name="d", working_dir="/tmp",
        origin="delegation", parent_session_id=octo_sess.id,
        delegation_request="r",
    )

    # Pretend the harness broadcast an event for the delegation child.
    # Patch all bridge handle_event calls so we'd see any leak.
    leaked: list[tuple[str, dict]] = []

    class FakeBridge:
        async def handle_event(self, chat_id, msg):
            leaked.append((chat_id, msg))

    bm._bridges["telegram"] = FakeBridge()

    await bm._on_broadcast({
        "type": "assistant_text", "session_id": delegation_child.id,
        "content": "leak?",
    })
    assert leaked == []


# ---------------------------------------------------------------------------
# HTTP routes
# ---------------------------------------------------------------------------


@pytest.fixture
async def client(db):
    """HTTP test client with the module-level singletons rebound to the
    per-test in-memory DB. Bridges/scheduler are stood up so app
    lifespan doesn't matter for these tests."""
    session_manager.sessions.clear()
    await session_manager.initialize(db)

    agents_mod.set_manager(AgentManager(db))
    runner = ScheduleRunner(session_manager, db)
    await runner.initialize()
    schedules_mod._db = db
    schedules_mod._runner = runner

    singleton_delegation_manager.bind(session_mgr=session_manager, db=db)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c

    singleton_delegation_manager.shutdown()
    # Drain the registry between tests so list_delegations doesn't leak
    # state into the next case.
    singleton_delegation_manager._records.clear()
    await runner.shutdown()


async def _post_agent(client, name, **extra):
    r = await client.post(
        "/api/agents", json={"name": name, **extra}, headers=HEADERS
    )
    assert r.status_code == 201, r.text
    return r.json()


async def _post_session(client, agent_id, name="parent"):
    r = await client.post(
        "/api/sessions",
        json={"name": name, "working_dir": "/tmp", "agent_id": agent_id},
        headers=HEADERS,
    )
    assert r.status_code == 201, r.text
    return r.json()


@pytest.mark.asyncio
async def test_route_start_delegation(client, monkeypatch):
    """POST /sessions/{sid}/delegations succeeds; child session shows
    up via GET /sessions/{child_sid}."""
    monkeypatch.setattr(session_manager, "start_message", _noop_start_message)

    agents = (await client.get("/api/agents", headers=HEADERS)).json()
    octo = next(a for a in agents if a["is_system"])
    await _post_agent(client, "Vera")
    parent = await _post_session(client, octo["id"])

    r = await client.post(
        f"/api/sessions/{parent['id']}/delegations",
        json={"agent_name": "vera", "request": "review the dashboard"},
        headers=HEADERS,
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["state"] == "running"
    assert body["target_agent_name"] == "Vera"
    assert body["parent_session_id"] == parent["id"]
    assert body["delegation_id"] == body["sub_session_id"]

    # Child session is visible via the normal GET route.
    cid = body["delegation_id"]
    detail = (await client.get(f"/api/sessions/{cid}", headers=HEADERS)).json()
    assert detail["origin"] == "delegation"
    assert detail["parent_session_id"] == parent["id"]
    assert detail["delegation_request"] == "review the dashboard"


@pytest.mark.asyncio
async def test_route_list_and_cancel(client, monkeypatch):
    monkeypatch.setattr(session_manager, "start_message", _noop_start_message)

    async def fake_interrupt(sid):
        return True

    monkeypatch.setattr(session_manager, "interrupt", fake_interrupt)

    agents = (await client.get("/api/agents", headers=HEADERS)).json()
    octo = next(a for a in agents if a["is_system"])
    await _post_agent(client, "Vera")
    parent = await _post_session(client, octo["id"])

    create = await client.post(
        f"/api/sessions/{parent['id']}/delegations",
        json={"agent_name": "vera", "request": "go"},
        headers=HEADERS,
    )
    did = create.json()["delegation_id"]

    listing = await client.get(
        f"/api/sessions/{parent['id']}/delegations", headers=HEADERS
    )
    assert listing.status_code == 200
    rows = listing.json()
    assert [r["delegation_id"] for r in rows] == [did]

    cancel = await client.post(
        f"/api/sessions/{parent['id']}/delegations/{did}/cancel",
        json={"reason": "test"},
        headers=HEADERS,
    )
    assert cancel.status_code == 200, cancel.text
    assert cancel.json()["state"] == "cancelled"


@pytest.mark.asyncio
async def test_route_404s(client, monkeypatch):
    monkeypatch.setattr(session_manager, "start_message", _noop_start_message)
    # Unknown parent session.
    r = await client.post(
        "/api/sessions/ghost/delegations",
        json={"agent_name": "vera", "request": "go"},
        headers=HEADERS,
    )
    assert r.status_code == 404

    # Known parent, unknown agent.
    agents = (await client.get("/api/agents", headers=HEADERS)).json()
    octo = next(a for a in agents if a["is_system"])
    parent = await _post_session(client, octo["id"])
    r = await client.post(
        f"/api/sessions/{parent['id']}/delegations",
        json={"agent_name": "ghost", "request": "go"},
        headers=HEADERS,
    )
    assert r.status_code == 404

    # Cancel an unknown delegation.
    r = await client.post(
        f"/api/sessions/{parent['id']}/delegations/nope/cancel",
        json={},
        headers=HEADERS,
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_route_self_delegation_409(client, monkeypatch):
    monkeypatch.setattr(session_manager, "start_message", _noop_start_message)
    agents = (await client.get("/api/agents", headers=HEADERS)).json()
    octo = next(a for a in agents if a["is_system"])
    parent = await _post_session(client, octo["id"])
    r = await client.post(
        f"/api/sessions/{parent['id']}/delegations",
        json={"agent_name": octo["name"], "request": "x"},
        headers=HEADERS,
    )
    assert r.status_code == 409


# ---------------------------------------------------------------------------
# Caller-aware ask: child question → parent injection (Phase 3)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_question_request_routed_to_parent(dm, mgr, db, monkeypatch):
    """A child raises a question; the manager injects
    `[agent-question:…]` into the parent. The pending question itself
    stays on the child's queue, intact — the parent's `answer` route
    drains it later."""
    injected: list[tuple[str, str]] = []

    async def capture(sid, prompt, attachment_ids=None):
        injected.append((sid, prompt))

    monkeypatch.setattr(mgr, "start_message", capture)
    octo = await db.get_system_agent()
    await _make_agent(db, "Vera")
    parent = await _make_session(mgr, octo["id"])
    rec = await dm.start_delegation(
        parent_session_id=parent.id, agent_name="vera", request="r",
    )
    injected.clear()

    await dm._on_broadcast({
        "type": "question_request",
        "session_id": rec.delegation_id,
        "question_id": "q-abc",
        "questions": [
            {
                "question": "Which file should I focus on?",
                "multiSelect": False,
                "options": [
                    {"label": "Dashboard.tsx", "description": "main UI"},
                    {"label": "Sidebar.tsx"},
                ],
            }
        ],
    })

    assert len(injected) == 1
    target_sid, prompt = injected[0]
    assert target_sid == parent.id
    # Prefix carries enough id-disambiguation for parallel delegations.
    assert "[agent-question:Vera" in prompt
    assert f"delegation={rec.delegation_id}" in prompt
    assert "question_id=q-abc" in prompt
    assert "Which file should I focus on?" in prompt
    assert "Dashboard.tsx" in prompt
    assert "Sidebar.tsx" in prompt
    assert "main UI" in prompt
    assert "single-choice" in prompt
    # The injection names the actual MCP tool the parent's model
    # should call (`mcp__ask_agent__answer`). Vera caught a
    # version of this prompt that referenced
    # `mcp__ask_agent__answer_agent_question` — a tool that
    # doesn't exist (the Python function name leaked into the
    # prompt). Guard the regression in both directions.
    assert "mcp__ask_agent__answer" in prompt
    assert "answer_agent_question" not in prompt
    # Delegation state stays running — question isn't a terminal event.
    assert rec.state == "running"


@pytest.mark.asyncio
async def test_question_with_empty_questions_does_not_crash(
    dm, mgr, db, monkeypatch
):
    """An edge case: the child emits a malformed question_request with
    no questions. We still inject a sensible placeholder so the parent
    can decide to cancel."""
    captured: list[tuple[str, str]] = []

    async def capture(sid, prompt, attachment_ids=None):
        captured.append((sid, prompt))

    monkeypatch.setattr(mgr, "start_message", capture)
    octo = await db.get_system_agent()
    await _make_agent(db, "Vera")
    parent = await _make_session(mgr, octo["id"])
    rec = await dm.start_delegation(
        parent_session_id=parent.id, agent_name="vera", request="r",
    )
    captured.clear()
    await dm._on_broadcast({
        "type": "question_request",
        "session_id": rec.delegation_id,
        "question_id": "q-x",
        "questions": [],
    })
    assert len(captured) == 1
    assert "empty payload" in captured[0][1]


@pytest.mark.asyncio
async def test_terminated_delegations_ignore_questions(
    dm, mgr, db, monkeypatch
):
    """A late question event after a child finished is dropped — the
    parent's reply has already been injected."""
    monkeypatch.setattr(mgr, "start_message", _noop_start_message)
    octo = await db.get_system_agent()
    await _make_agent(db, "Vera")
    parent = await _make_session(mgr, octo["id"])
    rec = await dm.start_delegation(
        parent_session_id=parent.id, agent_name="vera", request="r",
    )
    # Drive to terminal.
    await dm._on_broadcast({
        "type": "result", "session_id": rec.delegation_id, "is_error": False,
    })
    # Now a stray question_request should be a no-op.
    captured = []

    async def capture(sid, prompt, attachment_ids=None):
        captured.append((sid, prompt))

    monkeypatch.setattr(mgr, "start_message", capture)
    await dm._on_broadcast({
        "type": "question_request",
        "session_id": rec.delegation_id,
        "question_id": "q-stray",
        "questions": [{"question": "huh?", "options": []}],
    })
    assert captured == []


# ---------------------------------------------------------------------------
# answer_pending_question on the manager
# ---------------------------------------------------------------------------


def _seed_pending_question(
    child_session, question_id: str, questions: list[dict]
) -> None:
    """Wire a fake pending question onto a child session without going
    through the full create_pending_question flow (which would need a
    persistence path)."""
    from server.session_manager import PendingQuestion
    import asyncio as _asyncio

    child_session._pending_questions[question_id] = PendingQuestion(
        question_id=question_id, questions=questions
    )
    child_session._pending_question_events[question_id] = _asyncio.Event()


@pytest.mark.asyncio
async def test_answer_pending_question_happy(dm, mgr, db, monkeypatch):
    monkeypatch.setattr(mgr, "start_message", _noop_start_message)
    octo = await db.get_system_agent()
    await _make_agent(db, "Vera")
    parent = await _make_session(mgr, octo["id"])
    rec = await dm.start_delegation(
        parent_session_id=parent.id, agent_name="vera", request="r",
    )
    child = mgr.get_session(rec.delegation_id)
    _seed_pending_question(
        child, "q-1", [{"question": "X?", "options": [{"label": "A"}, {"label": "B"}]}]
    )

    # Capture answer_question call shape.
    called: dict = {}

    async def fake_answer(sid, qid, answers):
        called["sid"] = sid
        called["qid"] = qid
        called["answers"] = answers
        return True

    monkeypatch.setattr(mgr, "answer_question", fake_answer)

    out = await dm.answer_pending_question(rec.delegation_id, "A")
    assert out["ok"] is True
    assert out["question_id"] == "q-1"
    assert called["sid"] == rec.delegation_id
    assert called["qid"] == "q-1"
    assert called["answers"] == [{"selected": ["A"], "text": None}]


@pytest.mark.asyncio
async def test_answer_pending_question_pads_multi_question_batch(
    dm, mgr, db, monkeypatch
):
    monkeypatch.setattr(mgr, "start_message", _noop_start_message)
    octo = await db.get_system_agent()
    await _make_agent(db, "Vera")
    parent = await _make_session(mgr, octo["id"])
    rec = await dm.start_delegation(
        parent_session_id=parent.id, agent_name="vera", request="r",
    )
    child = mgr.get_session(rec.delegation_id)
    _seed_pending_question(
        child,
        "q-1",
        [
            {"question": "first?", "options": [{"label": "A"}]},
            {"question": "second?", "options": [{"label": "B"}]},
            {"question": "third?", "options": [{"label": "C"}]},
        ],
    )

    seen: dict = {}

    async def fake_answer(sid, qid, answers):
        seen["answers"] = answers
        return True

    monkeypatch.setattr(mgr, "answer_question", fake_answer)

    await dm.answer_pending_question(rec.delegation_id, "A")
    # First question gets the parent's choice; the rest get blank
    # selected, same shape the human UI sends when entries are skipped.
    assert seen["answers"][0] == {"selected": ["A"], "text": None}
    assert seen["answers"][1] == {"selected": [], "text": None}
    assert seen["answers"][2] == {"selected": [], "text": None}


@pytest.mark.asyncio
async def test_answer_pending_question_rejects_empty_choice(
    dm, mgr, db, monkeypatch
):
    monkeypatch.setattr(mgr, "start_message", _noop_start_message)
    octo = await db.get_system_agent()
    await _make_agent(db, "Vera")
    parent = await _make_session(mgr, octo["id"])
    rec = await dm.start_delegation(
        parent_session_id=parent.id, agent_name="vera", request="r",
    )
    child = mgr.get_session(rec.delegation_id)
    _seed_pending_question(child, "q-1", [{"question": "?", "options": []}])
    with pytest.raises(DelegationError) as ex:
        await dm.answer_pending_question(rec.delegation_id, "   ")
    assert ex.value.status_code == 400


@pytest.mark.asyncio
async def test_answer_pending_question_when_no_pending(
    dm, mgr, db, monkeypatch
):
    monkeypatch.setattr(mgr, "start_message", _noop_start_message)
    octo = await db.get_system_agent()
    await _make_agent(db, "Vera")
    parent = await _make_session(mgr, octo["id"])
    rec = await dm.start_delegation(
        parent_session_id=parent.id, agent_name="vera", request="r",
    )
    with pytest.raises(DelegationError) as ex:
        await dm.answer_pending_question(rec.delegation_id, "A")
    assert ex.value.status_code == 409


@pytest.mark.asyncio
async def test_answer_pending_question_after_terminal(
    dm, mgr, db, monkeypatch
):
    monkeypatch.setattr(mgr, "start_message", _noop_start_message)
    octo = await db.get_system_agent()
    await _make_agent(db, "Vera")
    parent = await _make_session(mgr, octo["id"])
    rec = await dm.start_delegation(
        parent_session_id=parent.id, agent_name="vera", request="r",
    )
    await dm._on_broadcast({
        "type": "result", "session_id": rec.delegation_id, "is_error": False,
    })
    with pytest.raises(DelegationError) as ex:
        await dm.answer_pending_question(rec.delegation_id, "A")
    assert ex.value.status_code == 409


@pytest.mark.asyncio
async def test_answer_pending_question_human_race_409(
    dm, mgr, db, monkeypatch
):
    """If session_manager.answer_question returns False (the human UI
    drained the queue first) we surface 409."""
    monkeypatch.setattr(mgr, "start_message", _noop_start_message)
    octo = await db.get_system_agent()
    await _make_agent(db, "Vera")
    parent = await _make_session(mgr, octo["id"])
    rec = await dm.start_delegation(
        parent_session_id=parent.id, agent_name="vera", request="r",
    )
    child = mgr.get_session(rec.delegation_id)
    _seed_pending_question(child, "q-1", [{"question": "?", "options": []}])

    async def fake_answer(*a, **k):
        return False

    monkeypatch.setattr(mgr, "answer_question", fake_answer)
    with pytest.raises(DelegationError) as ex:
        await dm.answer_pending_question(rec.delegation_id, "A")
    assert ex.value.status_code == 409


# ---------------------------------------------------------------------------
# HTTP: answer route
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_route_answer_happy(client, monkeypatch):
    monkeypatch.setattr(session_manager, "start_message", _noop_start_message)

    agents = (await client.get("/api/agents", headers=HEADERS)).json()
    octo = next(a for a in agents if a["is_system"])
    await _post_agent(client, "Vera")
    parent = await _post_session(client, octo["id"])

    create = await client.post(
        f"/api/sessions/{parent['id']}/delegations",
        json={"agent_name": "vera", "request": "go"},
        headers=HEADERS,
    )
    did = create.json()["delegation_id"]

    # Seed the child's pending-question queue directly + patch the
    # underlying answer_question to capture.
    child = session_manager.get_session(did)
    _seed_pending_question(
        child, "q-route", [{"question": "?", "options": [{"label": "Yes"}]}]
    )

    async def fake_answer(sid, qid, answers):
        assert sid == did and qid == "q-route"
        assert answers == [{"selected": ["Yes"], "text": None}]
        return True

    monkeypatch.setattr(session_manager, "answer_question", fake_answer)

    r = await client.post(
        f"/api/sessions/{parent['id']}/delegations/{did}/answer",
        json={"choice": "Yes"},
        headers=HEADERS,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["question_id"] == "q-route"


@pytest.mark.asyncio
async def test_route_answer_404_for_unknown_delegation(client, monkeypatch):
    monkeypatch.setattr(session_manager, "start_message", _noop_start_message)
    agents = (await client.get("/api/agents", headers=HEADERS)).json()
    octo = next(a for a in agents if a["is_system"])
    parent = await _post_session(client, octo["id"])
    r = await client.post(
        f"/api/sessions/{parent['id']}/delegations/nope/answer",
        json={"choice": "X"},
        headers=HEADERS,
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_route_answer_409_when_no_pending(client, monkeypatch):
    monkeypatch.setattr(session_manager, "start_message", _noop_start_message)
    agents = (await client.get("/api/agents", headers=HEADERS)).json()
    octo = next(a for a in agents if a["is_system"])
    await _post_agent(client, "Vera")
    parent = await _post_session(client, octo["id"])
    create = await client.post(
        f"/api/sessions/{parent['id']}/delegations",
        json={"agent_name": "vera", "request": "go"},
        headers=HEADERS,
    )
    did = create.json()["delegation_id"]
    r = await client.post(
        f"/api/sessions/{parent['id']}/delegations/{did}/answer",
        json={"choice": "X"},
        headers=HEADERS,
    )
    assert r.status_code == 409
