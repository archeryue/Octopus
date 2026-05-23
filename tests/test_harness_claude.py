"""Phase 2: the CLAUDE_CODE profile is a faithful port of the legacy
ClaudeCodeBackend — turn argv (asserted *equal* to the old backend's
build_args), event normalization, and the one-shot call.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from server.harness import assembly
from server.harness.claude_code import (
    _OCTOPUS_SYSTEM_PROMPT,
    ClaudeEventParser,
    build_oneshot_argv,
    build_turn_argv,
    parse_oneshot_stdout,
)
from server.harness.events import HarnessCredential, HarnessOneshotError
from server.harness.profile import OneShotContext, TurnContext


def _assemble_ctx(
    *, prompt, wd, resume, credential, model=None, mcp_servers=None,
    tool_allow=None, tool_deny=None, persona=None, session_id=None, connectors=(),
) -> TurnContext:
    abs_wd = str(Path(wd).resolve())
    cb = assembly.build_callback_env(session_id)
    entries = assembly.select_mcp_servers(mcp_servers, list(connectors), abs_wd, cb)
    sysp = assembly.compose_system_prompt(persona, _OCTOPUS_SYSTEM_PROMPT, list(connectors))
    return TurnContext(
        prompt=prompt, working_dir=abs_wd, resume_id=resume, system_prompt=sysp,
        model=model, tool_allow=tool_allow, tool_deny=tool_deny,
        mcp_servers=entries, credential=credential,
    )


def test_turn_argv_full_config(tmp_path):
    """A rich config renders the expected `claude --print` command (VM0 shape)
    + the oauth credential env. (The faithful-port-vs-legacy equivalence was
    proven during the migration; this is the standing snapshot.)"""
    import json

    ctx = _assemble_ctx(
        prompt="hello", wd=str(tmp_path), resume="resume-1",
        credential=HarnessCredential(backend="claude-code", auth_type="oauth", secret="tok-xyz"),
        model="claude-opus-4-7", mcp_servers=["viewer", "ask"],
        tool_allow=["Read", "Glob"], tool_deny=["Write"], persona="PERSONA", session_id="s1",
    )
    argv, kw = build_turn_argv(ctx)

    assert argv[0] == "claude"
    assert {"--print", "--output-format=stream-json", "--verbose", "--dangerously-skip-permissions"} <= set(argv)
    assert argv[argv.index("--model") + 1] == "claude-opus-4-7"
    assert argv[argv.index("--allowedTools") + 1] == "Read,Glob"
    assert argv[argv.index("--disallowedTools") + 1] == "AskUserQuestion,Write"
    assert argv[argv.index("--resume") + 1] == "resume-1"
    assert argv[-2:] == ["--", "hello"]
    cfg = json.loads(argv[argv.index("--mcp-config") + 1])["mcpServers"]
    assert set(cfg) == {"viewer", "ask"}  # only the selected built-ins
    ap = argv[argv.index("--append-system-prompt") + 1]
    assert ap.startswith("PERSONA") and "Octopus in-app tools" in ap
    assert kw["cwd"] == str(tmp_path)
    assert kw["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "tok-xyz"


def test_turn_argv_api_key_and_minimal(tmp_path):
    ctx = _assemble_ctx(
        prompt="p", wd=str(tmp_path), resume=None,
        credential=HarnessCredential(backend="claude-code", auth_type="api_key", secret="sk-ant-1"),
    )
    argv, kw = build_turn_argv(ctx)
    assert argv[0] == "claude" and argv[-2:] == ["--", "p"]
    assert "--resume" not in argv  # no resume id
    assert "--allowedTools" not in argv  # no allow list
    assert kw["env"]["ANTHROPIC_API_KEY"] == "sk-ant-1"
    # AskUserQuestion is always disabled so the model uses mcp__ask__user.
    di = argv.index("--disallowedTools")
    assert argv[di + 1] == "AskUserQuestion"


# --------------------------------------------------------------------------- #
# Event normalization
# --------------------------------------------------------------------------- #


def test_parser_full_turn():
    p = ClaudeEventParser()

    out = p.parse({"type": "system", "subtype": "init", "session_id": "abc"})
    assert [e.type for e in out.events] == ["session_started"]
    assert out.events[0].session_id == "abc"
    assert out.end_of_stream is False

    out = p.parse(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "hi"},
                    {"type": "text", "text": "   "},  # whitespace-only: skipped
                    {"type": "thinking", "thinking": "hmm"},
                    {"type": "tool_use", "name": "Read", "input": {"file": "x"}, "id": "t1"},
                ]
            },
        }
    )
    assert [e.type for e in out.events] == ["text", "thinking", "tool_use"]
    assert out.events[2].tool_name == "Read" and out.events[2].tool_use_id == "t1"

    out = p.parse(
        {
            "type": "user",
            "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok", "is_error": False}]},
        }
    )
    assert out.events[0].type == "tool_result" and out.events[0].content == "ok"

    out = p.parse({"type": "result", "total_cost_usd": 0.01, "num_turns": 2})
    assert out.end_of_stream is True
    assert out.events[0].type == "result"
    assert out.events[0].session_id == "abc"  # falls back to captured init id
    assert out.events[0].cost == 0.01


def test_parser_ignores_control_and_unknown():
    p = ClaudeEventParser()
    for kind in ("control_request", "control_response", "rate_limit_event", "stream_event", "mystery"):
        assert p.parse({"type": kind}).events == []


# --------------------------------------------------------------------------- #
# One-shot
# --------------------------------------------------------------------------- #


def test_oneshot_argv_and_parse(tmp_path):
    argv, kw = build_oneshot_argv(
        OneShotContext(
            prompt="parse this", model="claude-opus-4-7",
            credential=HarnessCredential(backend="claude-code", auth_type="api_key", secret="sk-1"),
            working_dir=str(tmp_path),
        )
    )
    assert argv == ["claude", "--print", "--output-format=json", "--model", "claude-opus-4-7", "--", "parse this"]
    assert kw["cwd"] == str(tmp_path)
    assert kw["env"]["ANTHROPIC_API_KEY"] == "sk-1"

    assert parse_oneshot_stdout('{"result":"the answer"}') == "the answer"
    assert parse_oneshot_stdout('{"result":""}') == ""
    with pytest.raises(HarnessOneshotError) as ei:
        parse_oneshot_stdout("not json")
    assert ei.value.code == "bad_output"
