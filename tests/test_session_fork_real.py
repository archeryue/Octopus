"""Real-CLI fork tests (session-rewind.md Phase 5 / §8).

Auto-skipped when the relevant binary (`claude` / `codex`) isn't on PATH.
These prove the things only a real CLI can: that Claude resumes natively from
the synthesized JSONL, that Codex's history-replay first turn carries the
pre-branch context and then switches to native resume on turn 2, and that the
safe-revert path leaves a recoverable git stash. They cost real model calls;
prompts that recall a planted word use the cheapest model.

Boots SessionManager (no FastAPI) the same way test_delegations_real.py does;
the bg/ask MCP servers launch but are never invoked by these prompts, so the
absent host callback doesn't matter.
"""

from __future__ import annotations

import asyncio
import glob
import os
import subprocess

import pytest

from server.agent_manager import AgentManager
from server.config import settings
from server.database import Database
from server.session_manager import SessionManager

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


async def _bootstrap(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "agents_dir", str(tmp_path / "agents"))
    db = Database(":memory:")
    await db.initialize()
    mgr = SessionManager()
    await mgr.initialize(db)
    am = AgentManager(db)
    # Disable agent durable memory for these tests. Long-term memory
    # legitimately SURVIVES a fork (it's a side effect, not conversation —
    # §5.6), so an agent that saved a word to memory in a parent turn could
    # recall it in the fork, confounding a test that checks the *conversation*
    # was rewound. Strip memory_dir from every RunConfig so the transcript is
    # the only source of context.
    _orig_run_config = mgr._run_config

    def _no_mem(session, agent=None, connectors=None):
        cfg = _orig_run_config(session, agent, connectors)
        cfg.memory_dir = None
        return cfg

    mgr._run_config = _no_mem  # type: ignore[assignment]
    return db, mgr, am


async def _run_turn(mgr, sid, prompt, timeout=180.0):
    """Fire a turn and wait for the session to go idle."""
    await mgr.start_message(sid, prompt)
    sess = mgr.get_session(sid)
    if sess and sess._active_task:
        await asyncio.wait_for(sess._active_task, timeout)


async def _turn_get_reply(mgr, db, sid, prompt, tries=6):
    """Run a turn and return the assistant text it produced. A small model
    (haiku / codex) intermittently emits an empty assistant turn for a tiny
    prompt — when it DOES answer it's correct, so we retry until there's text
    rather than asserting on a single flaky turn. Each retry re-asks; the
    fork's context (which never contains the rewound word) is unchanged."""
    for _ in range(tries):
        msgs = await db.load_messages(sid)
        before = msgs[-1]["seq"] if msgs else -1
        await _run_turn(mgr, sid, prompt)
        text = _assistant_text_after(await db.load_messages(sid), before)
        if text.strip():
            return text
    return ""


def _assistant_text_after(msgs, after_seq):
    return " ".join(
        m["content"]
        for m in msgs
        if m["role"] == "assistant"
        and m["type"] == "text"
        and m["seq"] > after_seq
        and isinstance(m["content"], str)
    )


def _user_seqs(msgs):
    return [m["seq"] for m in msgs if m["role"] == "user" and m["type"] == "text"]


# ---------------------------------------------------------------------------
# Claude — NATIVE_TRANSCRIPT resume
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not HAS_CLAUDE, reason="claude CLI not on PATH")
@pytest.mark.asyncio
async def test_claude_fork_resumes_pre_branch_context(tmp_path, monkeypatch):
    """Plant MARIGOLD (turn 1), then ZEPHYR (turn 2). Fork rewinding to turn 2.
    The fork must recall MARIGOLD (pre-branch) but not ZEPHYR (the rewound turn
    and later) — proving Claude resumes from the synthesized JSONL with exactly
    the copied prefix."""
    db, mgr, am = await _bootstrap(tmp_path, monkeypatch)
    wd = str(tmp_path / "ws")
    os.makedirs(wd, exist_ok=True)
    try:
        octo = await db.get_system_agent()
        sess = await mgr.create_session(agent_id=octo["id"], name="s",
                                        working_dir=wd, backend="claude-code")
        # Force the cheap model for these recall turns.
        await am.update_agent(octo["id"], model="haiku")

        _NO_TOOLS = " Do not use any tools or save anything; just reply in text."
        await _run_turn(mgr, sess.id,
                        "Remember this word: MARIGOLD. Reply only OK." + _NO_TOOLS)
        await _run_turn(mgr, sess.id,
                        "Now remember this word: ZEPHYR. Reply only OK." + _NO_TOOLS)
        # The 2nd user message (ZEPHYR) is the rewind target — read its actual
        # seq rather than predicting it (assistant/result rows sit between).
        msgs = await db.load_messages(sess.id)
        second_user_seq = _user_seqs(msgs)[1]

        # Fork rewinding to the ZEPHYR user message. Claude forks use
        # HISTORY_REPLAY (Phase-5 finding): the first turn replays the truncated
        # transcript into its prompt — no synth-resume race.
        fork = await mgr.fork_session(sess.id, second_user_seq)
        assert fork.fork_needs_replay is True
        assert fork.claude_session_id is None  # claude's own id arrives turn 1

        # The fork only carries the MARIGOLD turn (ZEPHYR was rewound past), so
        # asking for "the word" must yield MARIGOLD and can never yield ZEPHYR.
        reply = (await _turn_get_reply(
            mgr, db, fork.id,
            "What word did I ask you to remember? Reply with only that word.",
        )).upper()
        assert reply, "fork turn never produced assistant text"
        assert "MARIGOLD" in reply, f"fork lost pre-branch context: {reply!r}"
        assert "ZEPHYR" not in reply, f"fork leaked rewound-turn context: {reply!r}"
        # First replay turn captured claude's own session id; replay cleared.
        assert fork.fork_needs_replay is False
        assert fork.claude_session_id, "session_started didn't capture a resume id"
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# Codex — HISTORY_REPLAY first turn, then native resume on turn 2
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not HAS_CODEX, reason="codex CLI not on PATH")
@pytest.mark.asyncio
async def test_codex_fork_history_replay_then_native_resume(tmp_path, monkeypatch):
    db, mgr, am = await _bootstrap(tmp_path, monkeypatch)
    wd = str(tmp_path / "ws")
    os.makedirs(wd, exist_ok=True)
    try:
        await am.create_agent(name="Codey", backend="codex")
        agent = await db.get_agent_by_name("Codey")
        sess = await mgr.create_session(agent_id=agent["id"], name="s",
                                        working_dir=wd, backend="codex")

        await _run_turn(mgr, sess.id, "Remember this word: MARIGOLD. Reply only OK.")
        await _run_turn(mgr, sess.id, "Now remember this word: ZEPHYR. Reply only OK.")
        msgs = await db.load_messages(sess.id)
        second_user_seq = _user_seqs(msgs)[1]

        fork = await mgr.fork_session(sess.id, second_user_seq)
        assert fork.fork_needs_replay is True
        assert fork.claude_session_id is None  # thread id arrives on turn 1

        # First fork turn: history replayed into the user prompt.
        reply = (await _turn_get_reply(
            mgr, db, fork.id,
            "List every word I asked you to remember so far, comma-separated. "
            "Reply with only the words.",
        )).upper()
        # After the first result, the replay flag clears and the thread id is
        # captured (native resume from turn 2 onward).
        assert fork.fork_needs_replay is False
        assert fork.claude_session_id, "thread.started didn't capture a resume id"
        assert "MARIGOLD" in reply, f"fork lost pre-branch context: {reply!r}"
        assert "ZEPHYR" not in reply, f"fork leaked rewound-turn context: {reply!r}"

        # Turn 2: native resume against the captured thread (no replay wrap).
        before = fork.claude_session_id
        reply2 = (await _turn_get_reply(
            mgr, db, fork.id,
            "What was the FIRST word I asked you to remember? Reply with only it.",
        )).upper()
        assert fork.claude_session_id == before  # same thread, native resume
        assert "MARIGOLD" in reply2, (
            f"native resume lost the fork-prefix context: {reply2!r}"
        )
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# Safe-revert against a real git repo + a real file-editing turn (Claude)
# ---------------------------------------------------------------------------


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.mark.skipif(not HAS_CLAUDE, reason="claude CLI not on PATH")
@pytest.mark.asyncio
async def test_claude_fork_safe_revert_real_repo(tmp_path, monkeypatch):
    """A real turn writes a file into a clean git repo; forking at M=0 with
    revert restores the working tree and stashes the agent's edit."""
    db, mgr, am = await _bootstrap(tmp_path, monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "t")
    (repo / "seed.txt").write_text("seed\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    try:
        octo = await db.get_system_agent()
        await am.update_agent(octo["id"], model="haiku")
        sess = await mgr.create_session(agent_id=octo["id"], name="s",
                                        working_dir=str(repo), backend="claude-code")

        # Turn-start git anchor is captured while the tree is clean (seq 0).
        await _run_turn(
            mgr, sess.id,
            f"Use the Write tool to create a file at {repo / 'notes.txt'} "
            "containing exactly the text HELLO. Then reply only: done.",
        )
        assert (repo / "notes.txt").exists(), "the agent didn't create the file"

        # Fork at M=0 with revert — the seq-0 anchor was clean, so revert runs.
        fork = await mgr.fork_session(sess.id, 0, revert_files=True)
        import json

        rec = json.loads(fork.fork_revert_record)
        assert rec["status"] == "completed", rec
        assert not (repo / "notes.txt").exists(), "revert didn't restore the tree"
        stash = subprocess.run(["git", "stash", "list"], cwd=repo,
                               capture_output=True, text=True).stdout
        assert f"octopus: pre-fork stash {fork.id}" in stash
    finally:
        await db.close()
