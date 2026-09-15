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

A real FastAPI **is** served for each test, on an ephemeral port that
`settings.port` is pointed at, carrying the two routers the in-turn MCP
shims call back into: `questions` (for `mcp__ask__user`) and `delegations`
(for `mcp__ask_agent__*`). Without it those shims time out — "failed to
reach Octopus" — and any test whose model actually obeys an instruction to
call one of them cannot pass. Those routes resolve the
`session_manager` / `delegation_manager` module globals, so the bootstrap
re-points the *router modules'* names at this test's own instances
(monkeypatch, so it's undone per test) rather than binding the process-wide
singletons — the managers stay per-test-isolated while the HTTP path and the
test's direct calls still act on the same objects.
"""

from __future__ import annotations

import asyncio
import glob
import os

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


from tests.cli_gate import claude_cli_works, codex_cli_works

# Gate on the CLI being installed AND signed in — a logged-out binary would
# otherwise fail these with a confusing 401 instead of skipping.
HAS_CLAUDE = claude_cli_works()
HAS_CODEX = codex_cli_works()


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


async def _serve_callback_api() -> tuple[int, "uvicorn.Server", asyncio.Task]:
    """Serve the routes the in-turn MCP shims POST back to, on a free port.

    `mcp__ask__user` and `mcp__ask_agent__*` are real subprocesses making real
    HTTP calls to `http://127.0.0.1:{settings.port}` (see
    `harness.assembly.build_callback_env`). With nothing listening they fail
    with "failed to reach Octopus … timed out", which used to be misread as
    the model declining to call the tool.
    """
    import uvicorn
    from fastapi import FastAPI

    from server.routers import delegations as delegations_routes
    from server.routers import questions as questions_routes

    app = FastAPI()
    app.include_router(delegations_routes.router)
    app.include_router(questions_routes.router)

    config = uvicorn.Config(
        app, host="127.0.0.1", port=0, log_level="warning", lifespan="off"
    )
    server = uvicorn.Server(config)
    # Signal handlers belong to pytest, not to a server we start mid-test.
    server.install_signal_handlers = lambda: None  # type: ignore[method-assign]
    task = asyncio.create_task(server.serve())
    for _ in range(200):
        if server.started and server.servers:
            break
        await asyncio.sleep(0.05)
    else:  # pragma: no cover - a stuck uvicorn is a real failure, not a skip
        raise RuntimeError("callback API server never started")
    port = server.servers[0].sockets[0].getsockname()[1]
    return port, server, task


async def _bootstrap(tmp_path, monkeypatch):
    """Common per-test setup: per-test agents dir, in-memory DB, the
    SessionManager + DelegationManager **singletons** bound to it (the HTTP
    routes resolve those, so private instances would leave the MCP shims
    talking to a different object graph), an AgentManager, a working_dir for
    the child sessions to inherit, and a live callback API.

    Returns `(db, mgr, dm, am, wd, teardown)`; call `await teardown()` in the
    test's `finally`.
    """
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

    # Point the routers at THIS test's managers. They hold module-global
    # references to the process-wide singletons, which know nothing about this
    # in-memory DB; monkeypatch restores them after the test, so nothing leaks
    # into the rest of the suite.
    from server.routers import delegations as delegations_routes
    from server.routers import questions as questions_routes

    monkeypatch.setattr(delegations_routes, "session_manager", mgr)
    monkeypatch.setattr(delegations_routes, "delegation_manager", dm)
    monkeypatch.setattr(questions_routes, "session_manager", mgr)

    port, server, task = await _serve_callback_api()
    monkeypatch.setattr(settings, "port", port)

    async def teardown() -> None:
        server.should_exit = True
        try:
            await asyncio.wait_for(task, timeout=10.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            task.cancel()
        dm.shutdown()
        # Release any CLI process a finished turn is holding. Without this each
        # test leaves ~255MB of live `claude` behind for the rest of the run,
        # which is enough to get the suite OOM-killed (inline-steering.md §7).
        await mgr.stop_all_held_processes()
        await db.close()

    return db, mgr, dm, am, wd, teardown


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
    db, mgr, dm, am, wd, teardown = await _bootstrap(tmp_path, monkeypatch)
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
        await teardown()


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
    db, mgr, dm, am, wd, teardown = await _bootstrap(tmp_path, monkeypatch)
    try:
        octo = await db.get_system_agent()
        assert octo is not None
        # Sonnet, not haiku: the assertion here is about Octopus's routing
        # (child question → parent injection), and haiku paraphrases the
        # question instead of calling `mcp__ask__user` often enough that the
        # test used to skip itself on those runs. The model is a fixture
        # detail, so pick one that follows the STRICT INSTRUCTION reliably
        # and let a genuine routing regression fail loudly.
        await am.create_agent(name="Vera", model="sonnet", backend="claude-code")
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
                "STRICT INSTRUCTION: You must invoke the tool named "
                "`mcp__ask__user` exactly once before saying anything. "
                "Pass it this exact `questions` argument: "
                "[{\"question\": \"which color do you prefer?\", "
                "\"options\": [{\"label\": \"red\"}, "
                "{\"label\": \"blue\"}]}]. "
                "Do not write any natural-language paraphrase of the "
                "question before calling the tool. Do not answer the "
                "question yourself. Calling the tool is required."
            ),
        )

        # Wait either for a question injection or a terminal injection;
        # whichever arrives first is the one to assert on.
        await _wait_for(lambda: bool(captured), timeout=180.0)
        first_prompt = captured[0][1]
        # The child's question MUST come back as a question injection — that
        # is the behaviour under test. Anything else (a paraphrase, a plain
        # reply) is a real failure, not something to skip past.
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
        pytest.fail(
            "the child's question never reached the parent as an "
            "[agent-question:Vera …] injection — the caller-chain routing is "
            f"broken (or the model ignored a STRICT INSTRUCTION). Parent got: "
            f"{first_prompt[:300]!r}"
        )
    finally:
        await teardown()


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
    db, mgr, dm, am, wd, teardown = await _bootstrap(tmp_path, monkeypatch)
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
        await teardown()


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
    db, mgr, dm, am, wd, teardown = await _bootstrap(tmp_path, monkeypatch)
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
                "STRICT INSTRUCTION — two steps, no commentary:\n"
                "Step 1: invoke the tool `mcp__ask_agent__ask` with "
                "name=\"Pete\" and request=\"Reply with exactly the "
                "5 characters HOP-7 and nothing else. No prose.\". "
                "Do not write any prose before calling the tool.\n"
                "Step 2: when the [agent-reply:Pete ...] follow-up "
                "turn arrives carrying Pete's text, reply with the "
                "exact 5-character token HOP-7 — nothing else. Do "
                "not paraphrase. Do not use any other tools."
            ),
        )

        # Multi-turn under Vera. Two real LLM calls + one for Pete.
        # Allow generous time but cap so a runaway model doesn't park
        # the test indefinitely.
        #
        # Vera injects into Octo more than once, by design: `ask_agent` is
        # asynchronous, so her FIRST turn ends as soon as she's started Pete
        # ("Delegation started to Pete … awaiting their reply") and the token
        # can only appear in a LATER turn — the one she takes after Pete's
        # `[agent-reply:Pete …]` lands in her own session. So wait for the
        # token to show up in *any* of her injections rather than asserting
        # on whichever happened to arrive first.
        def _token_arrived() -> bool:
            return any("HOP-7" in prompt for _, prompt in captured)

        try:
            await _wait_for(_token_arrived, timeout=480.0)
        except AssertionError:
            # Dump where the chain actually stopped: which sessions exist,
            # what state each delegation reached, and what the child said.
            chain = [
                {
                    "id": rid,
                    "target": rec.target_agent_name,
                    "state": rec.state,
                    "error": rec.error,
                    "text": " ".join(rec.captured_text)[:200],
                }
                for rid, rec in dm._records.items()
            ]
            sessions = [
                (sess.name, sess.id, sess.status, sess.origin)
                for sess in mgr.sessions.values()
            ]
            raise AssertionError(
                "Pete's token never made it back up the chain to Octo.\n"
                f"  Vera's injections: {[p[:200] for _, p in captured]!r}\n"
                f"  Delegations: {chain!r}\n"
                f"  Sessions: {sessions!r}"
            ) from None
        # Every injection the chain produces is Vera reporting to her caller.
        assert all(
            prompt.startswith("[agent-reply:Vera ") for _, prompt in captured
        ), [p[:120] for _, p in captured]
    finally:
        await teardown()
