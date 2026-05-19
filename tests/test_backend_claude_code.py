"""Unit tests for ClaudeCodeBackend against a scripted fake CLI.

Doesn't require the real `claude` binary; uses tests/_fixtures/fake_claude_cli.py.

Note (VM0-shape migration): ClaudeCodeBackend no longer drives the
SDK control protocol over stdin. AskUserQuestion is handled by the
new `mcp__ask__user` MCP tool + REST long-poll (see
docs/cli-resume-synthetic-pair.md §17 for context). The tests in
this file therefore exercise:

  - the stdout JSONL parser (`_emit_assistant_blocks`,
    `_emit_user_blocks`, `_emit_result`)
  - `build_args` flag set + credential env handling
  - lifecycle (start/stream/stop, interrupt → stop)

The old control-protocol tests (AUQ deny channel, can_use_tool
callback dispatch, initialize handshake) are gone because those
code paths are gone.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from server.backends import BackendEvent, ClaudeCodeBackend

FAKE_CLI = Path(__file__).parent / "_fixtures" / "fake_claude_cli.py"


class _ScriptedClaudeCodeBackend(ClaudeCodeBackend):
    """ClaudeCodeBackend that runs our fake CLI in a chosen mode.

    Overrides build_args to launch the fake script directly instead
    of the real `claude` binary; the stream-parsing path under test
    doesn't care about which executable produced the JSONL."""

    def __init__(self, mode: str, **kwargs):
        super().__init__(**kwargs)
        self._mode = mode

    def build_args(self, prompt, working_dir, resume_id, credential=None):
        return (
            [sys.executable, str(FAKE_CLI), self._mode],
            {"cwd": working_dir},
        )


async def _drain(stream) -> list[BackendEvent]:
    out: list[BackendEvent] = []
    async for ev in stream:
        out.append(ev)
    return out


# ---------------------------------------------------------------------------
# Stream parsing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_simple_hello_emits_text_then_result(tmp_path):
    backend = _ScriptedClaudeCodeBackend("hello")
    await backend.start("hi", str(tmp_path))

    events = await asyncio.wait_for(_drain(backend.stream()), timeout=5.0)
    await backend.stop()

    assert [e.type for e in events] == ["session_started", "text", "result"]
    assert events[0].session_id == "11111111-1111-1111-1111-111111111111"
    assert events[1].content == "Hello back."
    assert events[2].session_id == "11111111-1111-1111-1111-111111111111"
    assert events[2].cost == 0.001


@pytest.mark.asyncio
async def test_tool_use_then_tool_result_then_text(tmp_path):
    backend = _ScriptedClaudeCodeBackend("tool-success")
    await backend.start("run the bash command", str(tmp_path))

    events = await asyncio.wait_for(_drain(backend.stream()), timeout=5.0)
    await backend.stop()

    types = [e.type for e in events]
    # session_started is surfaced on init so the recovery path in
    # session_manager has a resume id even when `result` never lands.
    assert types == ["session_started", "tool_use", "tool_result", "text", "result"]
    sess = events[0]
    assert sess.session_id == "11111111-1111-1111-1111-111111111111"
    tu = events[1]
    assert tu.tool_name == "Bash"
    assert tu.tool_input == {"command": "echo hi"}
    assert tu.tool_use_id == "toolu_xyz"
    tr = events[2]
    assert tr.tool_use_id == "toolu_xyz"
    assert tr.content == "hi"
    assert tr.is_error is False


@pytest.mark.asyncio
async def test_premature_exit_after_tool_emits_no_result(tmp_path):
    """Reproduce the CLI bug from docs/cli-resume-synthetic-pair.md:
    after a tool roundtrip the CLI exits without emitting `result`.
    The backend should expose this as a session_started + tool_use +
    tool_result sequence with NO `result` event — leaving recovery
    to the session_manager layer."""
    backend = _ScriptedClaudeCodeBackend("premature-exit-after-tool")
    await backend.start("read a big file", str(tmp_path))

    events = await asyncio.wait_for(_drain(backend.stream()), timeout=5.0)
    await backend.stop()

    types = [e.type for e in events]
    assert types == ["session_started", "tool_use", "tool_result"]
    assert events[0].session_id == "11111111-1111-1111-1111-111111111111"
    assert events[1].tool_use_id == "toolu_premature"
    assert events[2].tool_use_id == "toolu_premature"


# ---------------------------------------------------------------------------
# Lifecycle: interrupt → stop, answer_question becomes a no-op
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_interrupt_terminates_the_subprocess(tmp_path):
    """VM0 shape doesn't send a graceful interrupt control_request —
    interrupt() just calls stop(), which SIGTERMs/SIGKILLs the
    subprocess. We verify the call returns cleanly and the process
    handle is torn down."""
    backend = _ScriptedClaudeCodeBackend("interrupt-respond")
    await backend.start("start work", str(tmp_path))
    # Give the fake CLI a moment to be sitting on stdin.
    await asyncio.sleep(0.1)

    await asyncio.wait_for(backend.interrupt(), timeout=5.0)
    # Stream is closed, process gone.
    assert backend._process is None


@pytest.mark.asyncio
async def test_answer_question_returns_true_as_noop(tmp_path):
    """answer_question is retained on the interface for back-compat
    but is a no-op under the VM0 shape (AUQ flows through the
    mcp__ask__user MCP server + asyncio.Event in session_manager,
    not through the backend). Returns True regardless of input so
    callers that haven't been updated still see success."""
    backend = _ScriptedClaudeCodeBackend("hello")
    await backend.start("hi", str(tmp_path))
    async for _ in backend.stream():
        pass
    assert await backend.answer_question("nonexistent-q", "x") is True
    await backend.stop()


# ---------------------------------------------------------------------------
# build_args (VM0 shape)
# ---------------------------------------------------------------------------


def test_build_args_uses_vm0_shape():
    """The whole point of the refactor: positional argv prompt, no
    `--input-format=stream-json`, `--dangerously-skip-permissions`
    instead of `--permission-prompt-tool=stdio`, built-in
    AskUserQuestion disabled."""
    backend = ClaudeCodeBackend()
    argv, _ = backend.build_args("hello prompt", "/tmp", None, credential=None)

    # Positional prompt after `--`.
    assert argv[-1] == "hello prompt"
    assert argv[-2] == "--"

    # Required flags present.
    assert "--print" in argv
    assert "--output-format=stream-json" in argv
    assert "--verbose" in argv
    assert "--dangerously-skip-permissions" in argv

    # Built-in AUQ explicitly disabled.
    i = argv.index("--disallowedTools")
    assert argv[i + 1] == "AskUserQuestion"

    # Old stream-json input + control-protocol flags MUST be gone —
    # those were the bug-trigger surface.
    assert "--input-format=stream-json" not in argv
    assert not any(a.startswith("--permission-prompt-tool") for a in argv)
    assert not any(a.startswith("--permission-mode") for a in argv)


def test_build_args_registers_viewer_bg_ask_mcp_servers():
    backend = ClaudeCodeBackend(session_id="sess-xyz")
    argv, _ = backend.build_args("p", "/tmp", None)
    cfg_idx = argv.index("--mcp-config") + 1
    cfg = json.loads(argv[cfg_idx])
    servers = cfg.get("mcpServers", {})
    assert set(servers.keys()) == {"viewer", "bg", "ask"}
    # bg + ask need the session id for HTTP callbacks.
    assert servers["bg"]["env"]["OCTOPUS_SESSION_ID"] == "sess-xyz"
    assert servers["ask"]["env"]["OCTOPUS_SESSION_ID"] == "sess-xyz"
    # viewer just needs the working_dir for path sandboxing.
    assert servers["viewer"]["env"]["OCTOPUS_WORKING_DIR"] == "/tmp"


def test_build_args_system_prompt_mentions_ask_user():
    backend = ClaudeCodeBackend(session_id="s")
    argv, _ = backend.build_args("p", "/tmp", None)
    sp = argv[argv.index("--append-system-prompt") + 1]
    assert "mcp__ask__user" in sp
    # And it tells the model the built-in is disabled, not silently
    # swapped.
    assert "DISABLED" in sp or "disabled" in sp


def test_build_args_resolves_relative_working_dir_to_absolute(tmp_path, monkeypatch):
    """Relative working_dir must be resolved against FastAPI's cwd
    BEFORE being handed to MCP env / subprocess cwd. Otherwise the
    MCP server (a grandchild of FastAPI) resolves it against its own
    inherited cwd, producing a doubled path like `/parent/Octopus/Octopus`
    that doesn't exist. Production bug reproducer + regression test."""
    import json

    # Move the test's cwd to tmp_path so the relative `subdir` resolves
    # to a known absolute. Use mkdir to make sure the directory exists.
    real = tmp_path / "subdir"
    real.mkdir()
    monkeypatch.chdir(tmp_path)

    backend = ClaudeCodeBackend(session_id="s")
    argv, kwargs = backend.build_args("p", "subdir", None)

    # Subprocess cwd should be absolute.
    assert kwargs["cwd"] == str(real)
    # Viewer MCP env should also carry the absolute path.
    cfg = json.loads(argv[argv.index("--mcp-config") + 1])
    assert cfg["mcpServers"]["viewer"]["env"]["OCTOPUS_WORKING_DIR"] == str(real)


def test_build_args_passes_resume_id():
    backend = ClaudeCodeBackend()
    argv, _ = backend.build_args("p", "/tmp", "abc-123")
    i = argv.index("--resume")
    assert argv[i + 1] == "abc-123"


def test_build_args_injects_api_key_credential():
    from server.backends import BackendCredential

    backend = ClaudeCodeBackend()
    cred = BackendCredential(
        backend="claude-code", auth_type="api_key", secret="sk-test-123"
    )
    _argv, kwargs = backend.build_args("p", "/tmp", None, credential=cred)
    env = kwargs.get("env", {})
    assert env.get("ANTHROPIC_API_KEY") == "sk-test-123"


def test_build_args_injects_oauth_credential():
    from server.backends import BackendCredential

    backend = ClaudeCodeBackend()
    cred = BackendCredential(
        backend="claude-code", auth_type="oauth", secret="oauth-token-456"
    )
    _argv, kwargs = backend.build_args("p", "/tmp", None, credential=cred)
    env = kwargs.get("env", {})
    assert env.get("CLAUDE_CODE_OAUTH_TOKEN") == "oauth-token-456"


def test_build_args_no_credential_leaves_env_unchanged():
    backend = ClaudeCodeBackend()
    _argv, kwargs = backend.build_args("p", "/tmp", None)
    env = kwargs.get("env", {})
    # ANTHROPIC_API_KEY may or may not be set by the host shell — we only
    # care that we didn't *override* it from a None credential.
    import os as _os
    assert env.get("ANTHROPIC_API_KEY") == _os.environ.get("ANTHROPIC_API_KEY")
