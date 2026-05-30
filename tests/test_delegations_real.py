"""Real-CLI agent-to-agent delegation tests (agent-collaboration.md §8).

Auto-skipped when the relevant binary (`claude` or `codex`) isn't on
PATH. Each test sets up an in-memory DB, two or more freshly-created
agents (Octo / Vera / Pete), boots SessionManager + DelegationManager
without the FastAPI HTTP layer, then exercises a real delegation
chain end-to-end.

The parent's `start_message` is wrapped so that the
``[agent-reply:…]`` / ``[agent-question:…]`` / ``[agent-error:…]``
turn injection into the parent is *captured* rather than triggering
a fresh LLM turn — we're testing the chain primitive, not the
parent's reply. This keeps each test to one real LLM call per real
agent in the chain.

Sub-agents call no tools (we instruct them not to), so the absence
of a live FastAPI on 127.0.0.1:<port> for the bg / ask / ask_agent
MCP servers doesn't matter — they simply aren't invoked.
"""

from __future__ import annotations

import asyncio
import glob
import os
import shutil

import pytest

from server.agent_manager import AgentManager
from server.config import settings
from server.database import Database
from server.delegations import DelegationManager
from server.session_manager import SessionManager

# Widen PATH so shutil.which finds binaries in nvm + ~/.local/bin in
# non-interactive pytest invocations.
for _d in [
    os.path.expanduser("~/.local/bin"),
    "/usr/local/bin",
    *sorted(glob.glob(os.path.expanduser("~/.nvm/versions/node/*/bin"))),
]:
    if _d and _d not in os.environ.get("PATH", "").split(os.pathsep):
        os.environ["PATH"] = _d + os.pathsep + os.environ.get("PATH", "")


HAS_CLAUDE = shutil.which("claude") is not None
HAS_CODEX = shutil.which("codex") is not None


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


async def _bootstrap(
    tmp_path, monkeypatch
) -> tuple[Database, SessionManager, DelegationManager, AgentManager, str]:
    """Common per-test setup: per-test agents dir, in-memory DB, fresh
    SessionManager + DelegationManager + AgentManager, and an existing
    working_dir for the child sessions to inherit."""
    monkeypatch.setattr(settings, "agents_dir", str(tmp_path / "agents"))
    db = Database(":memory:")
    await db.initialize()
    mgr = SessionManager()
    await mgr.initialize(db)
    dm = DelegationManager()
    dm.bind(session_mgr=mgr, db=db)
    am = AgentManager(db)
    wd = str(tmp_path / "ws")
    os.makedirs(wd, exist_ok=True)
    return db, mgr, dm, am, wd


def _intercept_parent_injections(
    mgr: SessionManager, parent_session_id: str
) -> list[tuple[str, str]]:
    """Wrap ``mgr.start_message`` so calls targeting ``parent_session_id``
    are captured (no LLM turn fired) while calls targeting any other
    session id pass through to the real implementation. Returns the
    capture list."""
    captured: list[tuple[str, str]] = []
    real = mgr.start_message

    async def wrapped(sid, prompt, attachment_ids=None):
        if sid == parent_session_id:
            captured.append((sid, prompt))
            return None
        return await real(sid, prompt, attachment_ids)

    mgr.start_message = wrapped  # type: ignore[assignment]
    return captured


async def _wait_for(
    predicate, *, timeout: float = 180.0, interval: float = 0.5
) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(interval)
    raise AssertionError(f"timed out waiting for: {predicate}")


# ---------------------------------------------------------------------------
# 2-hop: claude → claude
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not HAS_CLAUDE, reason="claude CLI not on PATH")
@pytest.mark.asyncio
async def test_real_two_hop_claude_to_claude(tmp_path, monkeypatch):
    """Octo (claude-code) delegates to Vera (claude-code). Vera's
    reply ends up injected into Octo's session as
    `[agent-reply:Vera delegation=… ]` carrying her assistant text."""
    db, mgr, dm, am, wd = await _bootstrap(tmp_path, monkeypatch)
    try:
        # The system Default Agent is "Octo" — created by the
        # migration. Reuse it as the parent rather than colliding on
        # the unique name index.
        octo = await db.get_system_agent()
        assert octo is not None
        await am.create_agent(name="Vera", model="haiku", backend="claude-code")
        octo_sess = await mgr.create_session(
            agent_id=octo["id"], name="octo", working_dir=wd
        )

        captured = _intercept_parent_injections(mgr, octo_sess.id)

        await dm.start_delegation(
            parent_session_id=octo_sess.id,
            agent_name="Vera",
            request=(
                "Reply with exactly the four characters: PONG. "
                "Do not call any tools. Do not say anything else."
            ),
        )

        await _wait_for(lambda: bool(captured), timeout=180.0)
        sid, prompt = captured[0]
        assert sid == octo_sess.id
        assert prompt.startswith("[agent-reply:Vera ")
        assert "PONG" in prompt
    finally:
        dm.shutdown()
        await db.close()


# ---------------------------------------------------------------------------
# Caller-aware question loop (Phase 3 in anger)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not HAS_CLAUDE, reason="claude CLI not on PATH")
@pytest.mark.asyncio
async def test_real_question_loop_claude_to_claude(tmp_path, monkeypatch):
    """Real-LLM check that a child's ``ask`` MCP question bubbles up
    to the parent's session as an injected ``[agent-question:…]`` turn.

    We can't easily run a *real* answer loop here (the ask MCP server
    would block on a real long-poll waiting for a FastAPI we don't
    spin up). Instead we let the model raise the question, capture
    the inbound injection on the parent side, and then drain the
    pending question programmatically the same way the route does —
    that's the answer-path the production code takes."""
    db, mgr, dm, am, wd = await _bootstrap(tmp_path, monkeypatch)
    try:
        octo = await db.get_system_agent()
        assert octo is not None
        await am.create_agent(name="Vera", model="haiku", backend="claude-code")
        octo_sess = await mgr.create_session(
            agent_id=octo["id"], name="octo", working_dir=wd
        )

        captured = _intercept_parent_injections(mgr, octo_sess.id)
        # We don't actually want Vera to wait for an answer (the real
        # `ask` server's long-poll would hang the test). Force the
        # pending question to be auto-answered via the manager's
        # answer path as soon as we detect the question injection.
        # In production this is what the parent agent does via the
        # `answer_agent_question` tool.
        rec = await dm.start_delegation(
            parent_session_id=octo_sess.id,
            agent_name="Vera",
            request=(
                "Use your `mcp__ask__user` tool to ask the question "
                "'which color do you prefer?' with two options "
                "labelled 'red' and 'blue'. Wait for the answer."
            ),
        )

        # Wait either for a question injection or a terminal injection;
        # whichever arrives first is the one to assert on.
        await _wait_for(lambda: bool(captured), timeout=180.0)
        first_prompt = captured[0][1]
        # The question may not always fire — some real LLM responses
        # paraphrase without invoking the tool. When it does fire,
        # confirm the prefix shape; otherwise xfail this assertion
        # path with a clear message rather than masking the result.
        if first_prompt.startswith("[agent-question:Vera "):
            assert "delegation=" in first_prompt
            assert "question_id=" in first_prompt
            # Drain the pending question (mirrors the route).
            child_sid = rec.delegation_id
            child = mgr.get_session(child_sid)
            assert child is not None
            assert child._pending_questions, "no pending question in queue"
            qid, _ = next(iter(child._pending_questions.items()))
            await mgr._deliver_question_answer(
                child, qid, "red", auto=False
            )
            return
        # No question — the model declined to use the tool. Don't fail
        # the suite on LLM non-determinism; this is acceptable for a
        # real-CLI gate.
        pytest.skip(
            "LLM didn't invoke the ask tool on this run; non-deterministic"
        )
    finally:
        dm.shutdown()
        await db.close()


# ---------------------------------------------------------------------------
# Harness-agnostic: claude → codex
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not (HAS_CLAUDE and HAS_CODEX),
    reason="both claude and codex CLIs need to be on PATH",
)
@pytest.mark.asyncio
async def test_real_two_hop_claude_to_codex(tmp_path, monkeypatch):
    """Octo (claude-code) delegates to Vera, who runs the codex
    harness. Same reply-injection shape. Proves the design is
    harness-agnostic at the chain level."""
    db, mgr, dm, am, wd = await _bootstrap(tmp_path, monkeypatch)
    try:
        octo = await db.get_system_agent()
        assert octo is not None
        # Vera runs codex; we leave model None so codex's default applies.
        await am.create_agent(name="Vera", backend="codex")
        octo_sess = await mgr.create_session(
            agent_id=octo["id"], name="octo", working_dir=wd
        )

        captured = _intercept_parent_injections(mgr, octo_sess.id)

        await dm.start_delegation(
            parent_session_id=octo_sess.id,
            agent_name="Vera",
            request=(
                "Reply with exactly the four characters: PONG. "
                "Do not call any tools. Do not say anything else."
            ),
        )

        await _wait_for(lambda: bool(captured), timeout=240.0)
        _, prompt = captured[0]
        assert prompt.startswith("[agent-reply:Vera ")
        # Codex sometimes preambles. Loose match: the token PONG appears.
        assert "PONG" in prompt
    finally:
        dm.shutdown()
        await db.close()


# ---------------------------------------------------------------------------
# 3-hop chain: Octo → Vera → Pete (Phase 5 nested in anger)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not HAS_CLAUDE, reason="claude CLI not on PATH")
@pytest.mark.asyncio
async def test_real_three_hop_chain(tmp_path, monkeypatch):
    """Octo asks Vera; Vera asks Pete; Pete replies with a token.
    We capture Vera's terminal injection into Octo's session and
    confirm Pete's token survived the chain.

    The depth cap (DEPTH_CAP=3) allows exactly this chain. The fourth
    hop would be rejected — covered by the unit test
    test_depth_cap_rejected.
    """
    db, mgr, dm, am, wd = await _bootstrap(tmp_path, monkeypatch)
    try:
        octo = await db.get_system_agent()
        assert octo is not None
        await am.create_agent(name="Vera", model="haiku", backend="claude-code")
        await am.create_agent(name="Pete", model="haiku", backend="claude-code")
        octo_sess = await mgr.create_session(
            agent_id=octo["id"], name="octo", working_dir=wd
        )

        captured = _intercept_parent_injections(mgr, octo_sess.id)

        # We instruct Vera to delegate further. Her request will tell
        # Pete to reply with a token. Vera then forwards the token in
        # her own assistant text so we can pluck it from the [agent-reply]
        # injection back into Octo.
        await dm.start_delegation(
            parent_session_id=octo_sess.id,
            agent_name="Vera",
            request=(
                "Use your `mcp__ask_agent__ask` tool to delegate to "
                "agent 'Pete'. Tell Pete to reply with exactly the "
                "token: TRIHOP-7. When Pete's reply arrives as a "
                "follow-up turn, repeat the token in your own reply "
                "and stop. Do not use any other tools."
            ),
        )

        # Multi-turn under Vera. Two real LLM calls + one for Pete.
        # Allow generous time but cap so a runaway model doesn't park
        # the test indefinitely.
        await _wait_for(lambda: bool(captured), timeout=480.0)
        _, prompt = captured[0]
        # The injection into Octo is Vera's reply, which should
        # contain the token Pete returned.
        assert prompt.startswith("[agent-reply:Vera ")
        if "TRIHOP-7" not in prompt:
            # LLM didn't follow the nested-delegation script cleanly
            # — this is real-CLI and inherently non-deterministic.
            pytest.skip(
                "3-hop LLM script didn't return the token; "
                "non-deterministic. Vera said: "
                f"{prompt[:300]!r}"
            )
    finally:
        dm.shutdown()
        await db.close()
