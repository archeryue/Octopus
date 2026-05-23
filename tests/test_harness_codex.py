"""Phase 3: the CODEX profile is a faithful port of the legacy CodexBackend —
turn argv (asserted *equal* to the old backend's build_args), event
normalization — plus the new D2 one-shot (codex exec → final agent_message).
"""

from __future__ import annotations

import json
from pathlib import Path

from server.harness import assembly
from server.harness.codex import (
    _OCTOPUS_SYSTEM_PROMPT_CODEX,
    CodexEventParser,
    build_oneshot_argv,
    build_turn_argv,
    parse_oneshot_stdout,
)
from server.harness.events import HarnessCredential
from server.harness.profile import OneShotContext, TurnContext


def _assemble_ctx(*, prompt, wd, resume, credential, model=None, mcp_servers=None, persona=None, session_id=None):
    abs_wd = str(Path(wd).resolve())
    cb = assembly.build_callback_env(session_id)
    entries = assembly.select_mcp_servers(mcp_servers, [], abs_wd, cb)
    sysp = assembly.compose_system_prompt(persona, _OCTOPUS_SYSTEM_PROMPT_CODEX, [])
    return TurnContext(
        prompt=prompt, working_dir=abs_wd, resume_id=resume, system_prompt=sysp,
        model=model, tool_allow=None, tool_deny=None, mcp_servers=entries, credential=credential,
    )


def test_turn_argv_full_config(tmp_path):
    """A rich config renders the expected `codex exec --json` command + the
    CODEX_HOME credential dir. (Faithful-port equivalence vs the legacy backend
    was proven during the migration; this is the standing snapshot.)"""
    home = str(tmp_path / "codexhome")
    ctx = _assemble_ctx(
        prompt="hello", wd=str(tmp_path), resume="resume-1",
        credential=HarnessCredential(backend="codex", auth_type="oauth", home_dir=home),
        model="gpt-5.5", mcp_servers=["viewer", "ask"], persona="PERSONA", session_id="s1",
    )
    argv, kw = build_turn_argv(ctx)
    joined = " ".join(argv)

    assert argv[:3] == ["codex", "exec", "--json"]
    assert "--dangerously-bypass-approvals-and-sandbox" in argv
    assert argv[argv.index("-C") + 1] == str(tmp_path)
    di = next(a for a in argv if a.startswith("developer_instructions="))
    assert "PERSONA" in di and "Octopus in-app tools" in di
    # Only the selected built-ins are rendered as -c overrides.
    assert "mcp_servers.viewer.command=" in joined and "mcp_servers.ask.command=" in joined
    assert "mcp_servers.bg.command=" not in joined
    assert argv[argv.index("-m") + 1] == "gpt-5.5"
    assert argv[argv.index("resume") + 1] == "resume-1"
    assert argv[-2:] == ["--", "hello"]
    assert kw["env"]["CODEX_HOME"] == home


def test_turn_argv_new_turn_and_no_home(tmp_path):
    ctx = _assemble_ctx(
        prompt="p", wd=str(tmp_path), resume=None,
        credential=HarnessCredential(backend="codex", auth_type="oauth", home_dir=None),
    )
    argv, kw = build_turn_argv(ctx)
    assert argv[:3] == ["codex", "exec", "--json"]
    assert "resume" not in argv  # new turn
    assert argv[-2:] == ["--", "p"]
    assert "CODEX_HOME" not in kw["env"]  # no home_dir → inherit host default


# --------------------------------------------------------------------------- #
# Event normalization
# --------------------------------------------------------------------------- #


def test_parser_thread_items_and_completion():
    p = CodexEventParser()

    out = p.parse({"type": "thread.started", "thread_id": "th-1"})
    assert out.events[0].type == "session_started" and out.events[0].session_id == "th-1"

    out = p.parse({"type": "turn.started"})
    assert out.events == [] and out.end_of_stream is False

    out = p.parse({"type": "item.completed", "item": {"type": "agent_message", "text": "hello"}})
    assert out.events[0].type == "text" and out.events[0].content == "hello"

    out = p.parse({
        "type": "item.started",
        "item": {"type": "mcp_tool_call", "id": "i1", "server": "viewer", "tool": "show_file", "arguments": {"path": "x"}},
    })
    assert out.events[0].type == "tool_use" and out.events[0].tool_name == "mcp__viewer__show_file"

    out = p.parse({"type": "turn.completed", "usage": {"input_tokens": 5}})
    assert out.end_of_stream is True
    assert out.events[0].type == "result" and out.events[0].session_id == "th-1"


def test_parser_turn_failed_and_error():
    p = CodexEventParser()
    out = p.parse({"type": "turn.failed", "error": {"message": "boom"}})
    assert out.end_of_stream is True
    assert out.events[0].type == "result" and out.events[0].is_error and out.events[0].content == "boom"

    out = p.parse({"type": "error", "message": "bad"})
    assert out.events[0].type == "error" and out.events[0].is_error
    assert out.end_of_stream is False  # a stray error doesn't end the turn


def test_parser_command_execution():
    p = CodexEventParser()
    out = p.parse({"type": "item.started", "item": {"type": "command_execution", "id": "c1", "command": "ls"}})
    assert out.events[0].type == "tool_use" and out.events[0].tool_name == "Bash"
    out = p.parse({"type": "item.completed", "item": {"type": "command_execution", "id": "c1", "output": "files", "exit_code": 0}})
    assert out.events[0].type == "tool_result" and out.events[0].is_error is False


# --------------------------------------------------------------------------- #
# One-shot (D2)
# --------------------------------------------------------------------------- #


def test_oneshot_argv(tmp_path):
    home = str(tmp_path / "h")
    argv, kw = build_oneshot_argv(
        OneShotContext(
            prompt="parse this", model="gpt-5.5",
            credential=HarnessCredential(backend="codex", auth_type="oauth", home_dir=home),
            working_dir=str(tmp_path),
        )
    )
    assert argv[:3] == ["codex", "exec", "--json"]
    assert "-C" in argv and argv[argv.index("-C") + 1] == str(tmp_path)
    assert argv[-4:] == ["-m", "gpt-5.5", "--", "parse this"]
    assert kw["env"]["CODEX_HOME"] == home


def test_oneshot_extracts_agent_message():
    stream = "\n".join([
        json.dumps({"type": "thread.started", "thread_id": "t"}),
        json.dumps({"type": "item.completed", "item": {"type": "reasoning", "text": "thinking"}}),
        json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": '{"name":"x"}'}}),
        json.dumps({"type": "turn.completed", "usage": {}}),
    ])
    assert parse_oneshot_stdout(stream) == '{"name":"x"}'
    assert parse_oneshot_stdout("garbage\n{not json") == ""  # nothing extractable
