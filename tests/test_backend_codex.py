"""Unit tests for CodexBackend against a scripted fake CLI.

Doesn't require the real `codex` binary (or a ChatGPT login) — uses
tests/_fixtures/fake_codex_cli.py. Exercises:

  - the `codex exec --json` event normalizer (on_stdout_line → BackendEvent),
    transcribed from VM0 + codex-backend.md §5.2
  - `build_args` flag set, ordering, MCP `-c` overrides, CODEX_HOME, resume

A live, logged-in confirmation pass (Phase C, codex-backend.md §12) still
gates "done" for the auth + real-CLI-honors-the-config questions — that needs
the user's ChatGPT subscription, which CI/this env can't exercise.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from server.backends import BackendEvent, CodexBackend

FAKE_CLI = Path(__file__).parent / "_fixtures" / "fake_codex_cli.py"


class _ScriptedCodexBackend(CodexBackend):
    """CodexBackend that runs the fake CLI in a chosen mode."""

    def __init__(self, mode: str, **kwargs):
        super().__init__(**kwargs)
        self._mode = mode

    def build_args(self, prompt, working_dir, resume_id, credential=None):
        return ([sys.executable, str(FAKE_CLI), self._mode], {"cwd": working_dir})


async def _drain(stream) -> list[BackendEvent]:
    out: list[BackendEvent] = []
    async for ev in stream:
        out.append(ev)
    return out


# ---------------------------------------------------------------------------
# Event normalization
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hello_emits_session_text_result(tmp_path):
    backend = _ScriptedCodexBackend("hello")
    await backend.start("hi", str(tmp_path))
    events = await asyncio.wait_for(_drain(backend.stream()), timeout=5.0)
    await backend.stop()

    assert [e.type for e in events] == ["session_started", "text", "result"]
    assert events[0].session_id == "thr_00000000000000000000000000"
    assert events[1].content == "Hello from Codex."
    # Codex reports tokens, not USD — cost stays None.
    assert events[2].cost is None
    assert events[2].session_id == "thr_00000000000000000000000000"


@pytest.mark.asyncio
async def test_command_execution_maps_to_bash_tool(tmp_path):
    backend = _ScriptedCodexBackend("tool")
    await backend.start("run it", str(tmp_path))
    events = await asyncio.wait_for(_drain(backend.stream()), timeout=5.0)
    await backend.stop()

    assert [e.type for e in events] == [
        "session_started",
        "tool_use",
        "tool_result",
        "text",
        "result",
    ]
    tu = events[1]
    assert tu.tool_name == "Bash"
    assert tu.tool_use_id == "c1"
    assert tu.tool_input == {"command": "echo hi"}
    tr = events[2]
    assert tr.tool_use_id == "c1"
    assert tr.content == "hi\n"
    assert tr.is_error is False


@pytest.mark.asyncio
async def test_file_ops_and_change_summary(tmp_path):
    backend = _ScriptedCodexBackend("files")
    await backend.start("write a file", str(tmp_path))
    events = await asyncio.wait_for(_drain(backend.stream()), timeout=5.0)
    await backend.stop()

    types = [e.type for e in events]
    assert types == ["session_started", "tool_use", "tool_result", "text", "result"]
    assert events[1].tool_name == "Write"
    assert events[1].tool_input == {"file_path": "/tmp/out.txt"}
    assert events[2].content == "+hello"
    assert "add: /tmp/out.txt" in events[3].content


@pytest.mark.asyncio
async def test_mcp_tool_call_maps_to_namespaced_tool(tmp_path):
    """Codex `mcp_tool_call` items (confirmed shape from a live Phase C run)
    map to mcp__<server>__<tool> tool_use + tool_result, matching Claude's
    naming so the frontend's viewer-dialog trigger fires for Codex too."""
    backend = _ScriptedCodexBackend("mcp")
    await backend.start("open it", str(tmp_path))
    events = await asyncio.wait_for(_drain(backend.stream()), timeout=5.0)
    await backend.stop()

    assert [e.type for e in events] == [
        "session_started",
        "tool_use",
        "tool_result",
        "text",
        "result",
    ]
    tu = events[1]
    assert tu.tool_name == "mcp__viewer__show_file"
    assert tu.tool_use_id == "t1"
    assert tu.tool_input == {"path": "hello.txt"}
    tr = events[2]
    assert tr.tool_use_id == "t1"
    assert tr.content == "Opened hello.txt (text, 14 bytes)."
    assert tr.is_error is False


@pytest.mark.asyncio
async def test_turn_failed_maps_to_error_result(tmp_path):
    backend = _ScriptedCodexBackend("failed")
    await backend.start("go", str(tmp_path))
    events = await asyncio.wait_for(_drain(backend.stream()), timeout=5.0)
    await backend.stop()

    assert [e.type for e in events] == ["session_started", "result"]
    assert events[1].is_error is True
    assert events[1].content == "model refused"


@pytest.mark.asyncio
async def test_error_event_maps_to_error(tmp_path):
    backend = _ScriptedCodexBackend("error")
    await backend.start("go", str(tmp_path))
    # No turn.completed → close the stream after the error so _drain returns.
    events: list[BackendEvent] = []
    async def collect():
        async for ev in backend.stream():
            events.append(ev)
            if ev.type == "error":
                backend._close_stream()
    await asyncio.wait_for(collect(), timeout=5.0)
    await backend.stop()

    assert any(e.type == "error" and e.is_error for e in events)
    assert events[0].content == "unrecoverable codex error"


# ---------------------------------------------------------------------------
# build_args
# ---------------------------------------------------------------------------


def test_build_args_exec_flags_and_order():
    """exec-level flags must precede the prompt; `--json`,
    `--dangerously-bypass-approvals-and-sandbox`, `--skip-git-repo-check`,
    `-C <abs_dir>` present (codex-backend.md §5.6)."""
    backend = CodexBackend(session_id="s")
    argv, kwargs = backend.build_args("do the thing", "/tmp", None)

    assert argv[0] == "codex"
    assert argv[1] == "exec"
    assert "--json" in argv
    assert "--dangerously-bypass-approvals-and-sandbox" in argv
    assert "--skip-git-repo-check" in argv
    ci = argv.index("-C")
    assert argv[ci + 1] == "/tmp"
    # Prompt is the final positional after `--`.
    assert argv[-2] == "--"
    assert argv[-1] == "do the thing"


def test_build_args_injects_developer_instructions():
    backend = CodexBackend(session_id="s")
    argv, _ = backend.build_args("p", "/tmp", None)
    di = next(a for a in argv if a.startswith("developer_instructions="))
    assert "mcp__ask__user" in di
    assert "Octopus in-app tools" in di


def test_build_args_agent_prompt_prepended_to_instructions():
    backend = CodexBackend(session_id="s", system_prompt="You are a pirate.")
    argv, _ = backend.build_args("p", "/tmp", None)
    di = next(a for a in argv if a.startswith("developer_instructions="))
    assert "You are a pirate." in di


def test_build_args_registers_mcp_servers_via_c_overrides():
    backend = CodexBackend(session_id="sess-xyz")
    argv, _ = backend.build_args("p", "/tmp", None)
    joined = "\n".join(argv)
    # Each of the three servers registered via -c mcp_servers.<key>.*
    for key in ("viewer", "bg", "ask"):
        assert f"mcp_servers.{key}.command=" in joined
        assert f"mcp_servers.{key}.args=" in joined
    # bg + ask carry the session id for HTTP callbacks.
    assert 'mcp_servers.bg.env.OCTOPUS_SESSION_ID="sess-xyz"' in joined
    assert 'mcp_servers.ask.env.OCTOPUS_SESSION_ID="sess-xyz"' in joined
    # viewer carries the working_dir for path sandboxing.
    assert 'mcp_servers.viewer.env.OCTOPUS_WORKING_DIR="/tmp"' in joined


def test_build_args_mcp_subset_selection():
    backend = CodexBackend(session_id="s", mcp_servers=["ask"])
    argv, _ = backend.build_args("p", "/tmp", None)
    joined = "\n".join(argv)
    assert "mcp_servers.ask.command=" in joined
    assert "mcp_servers.bg.command=" not in joined
    assert "mcp_servers.viewer.command=" not in joined


def test_build_args_resume_is_a_subcommand_before_prompt():
    backend = CodexBackend(session_id="s")
    argv, _ = backend.build_args("continue please", "/tmp", "thr_123")
    ri = argv.index("resume")
    assert argv[ri + 1] == "thr_123"
    assert argv[ri + 2] == "--"
    assert argv[ri + 3] == "continue please"


def test_build_args_sets_codex_home_when_credential_home_given(tmp_path):
    home = str(tmp_path / "codexhome")
    backend = CodexBackend(session_id="s", credential_home=home)
    _argv, kwargs = backend.build_args("p", "/tmp", None)
    assert kwargs["env"]["CODEX_HOME"] == home


def test_build_args_no_credential_home_inherits_host_codex_home(tmp_path):
    backend = CodexBackend(session_id="s")
    _argv, kwargs = backend.build_args("p", "/tmp", None)
    # We don't force CODEX_HOME — host's default ~/.codex is inherited.
    import os as _os

    assert kwargs["env"].get("CODEX_HOME") == _os.environ.get("CODEX_HOME")


def test_build_args_passes_model():
    backend = CodexBackend(session_id="s", model="gpt-5-codex")
    argv, _ = backend.build_args("p", "/tmp", None)
    mi = argv.index("-m")
    assert argv[mi + 1] == "gpt-5-codex"


def test_codex_opts_out_of_premature_exit_recovery():
    assert CodexBackend().wants_premature_exit_recovery is False


def test_build_args_resolves_relative_working_dir(tmp_path, monkeypatch):
    real = tmp_path / "wd"
    real.mkdir()
    monkeypatch.chdir(tmp_path)
    backend = CodexBackend(session_id="s")
    argv, kwargs = backend.build_args("p", "wd", None)
    assert kwargs["cwd"] == str(real)
    ci = argv.index("-C")
    assert argv[ci + 1] == str(real)
