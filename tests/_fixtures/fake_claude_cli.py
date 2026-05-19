#!/usr/bin/env python3
"""Fake `claude` CLI for ClaudeCodeBackend tests.

Mirrors the VM0-shape `claude --print` flow that Octopus now uses:
the prompt is passed as a positional argv (we ignore it; the mode
arg drives the scripted output), and **no stdin protocol** is read
or driven. We just emit scripted JSONL events on stdout and exit.

The control-protocol modes from the pre-refactor era (initialize
handshake, can_use_tool round-trip, interrupt control_request) are
gone — those code paths no longer exist in ClaudeCodeBackend.

Invocation:
    python fake_claude_cli.py <mode> [-- <prompt>]

Modes:
  hello                   : simple text + result.
  tool-success            : assistant tool_use + user tool_result + text + result.
  interrupt-respond       : blocks on a long sleep so tests can verify
                            interrupt() → stop() actually tears the process
                            down via SIGTERM/SIGKILL.
  premature-exit-after-tool : reproduces the CLI bug documented in
                            docs/cli-resume-synthetic-pair.md — emits
                            init + tool_use + tool_result and then
                            exits WITHOUT emitting `result`, simulating
                            the model never being re-invoked.
"""

import json
import os
import sys
import time


SESSION_ID = "11111111-1111-1111-1111-111111111111"


def _emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _emit_init():
    _emit({
        "type": "system",
        "subtype": "init",
        "cwd": os.getcwd(),
        "session_id": SESSION_ID,
        "tools": ["Bash", "Read"],
        "model": "claude-haiku-4-5-20251001",
        "permissionMode": "bypassPermissions",
    })


def _emit_result(*, subtype="success", cost=0.001):
    _emit({
        "type": "result",
        "subtype": subtype,
        "is_error": subtype != "success",
        "duration_ms": 100,
        "num_turns": 1,
        "session_id": SESSION_ID,
        "total_cost_usd": cost,
    })


def run_hello():
    _emit_init()
    _emit({
        "type": "assistant",
        "message": {
            "model": "claude-haiku-4-5-20251001",
            "id": "msg_test_1",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": "Hello back."}],
        },
        "session_id": SESSION_ID,
    })
    _emit_result()


def run_tool_success():
    _emit_init()
    _emit({
        "type": "assistant",
        "message": {
            "model": "claude-haiku-4-5-20251001",
            "id": "msg_t1",
            "role": "assistant",
            "content": [{
                "type": "tool_use",
                "id": "toolu_xyz",
                "name": "Bash",
                "input": {"command": "echo hi"},
            }],
        },
    })
    _emit({
        "type": "user",
        "message": {
            "role": "user",
            "content": [{
                "type": "tool_result",
                "tool_use_id": "toolu_xyz",
                "content": "hi",
                "is_error": False,
            }],
        },
    })
    _emit({
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "Done."}],
        },
    })
    _emit_result()


def run_premature_exit_after_tool():
    """Reproduce the CLI premature-exit-after-tool bug: emit init,
    then a tool_use, then the tool_result echo, then exit WITHOUT
    emitting `result`. This is the exact event shape that left
    session 561d86e2-… stuck — see docs/cli-resume-synthetic-pair.md."""
    _emit_init()
    _emit({
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{
                "type": "tool_use",
                "id": "toolu_premature",
                "name": "Read",
                "input": {"file_path": "/some/large/file.md"},
            }],
        },
    })
    _emit({
        "type": "user",
        "message": {
            "role": "user",
            "content": [{
                "type": "tool_result",
                "tool_use_id": "toolu_premature",
                "content": "...large file content...",
                "is_error": False,
            }],
        },
    })
    # NO result event — process just exits.


def run_interrupt_respond():
    """Emit the init banner, then sleep for a long time so the test
    can call interrupt() and verify the subprocess actually gets
    torn down. We sleep in 0.1 s chunks so SIGTERM lands promptly."""
    _emit_init()
    for _ in range(600):  # up to 60 seconds of waiting
        time.sleep(0.1)


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "hello"
    if mode == "hello":
        run_hello()
    elif mode == "tool-success":
        run_tool_success()
    elif mode == "interrupt-respond":
        run_interrupt_respond()
    elif mode == "premature-exit-after-tool":
        run_premature_exit_after_tool()
    else:
        sys.stderr.write(f"unknown mode: {mode}\n")
        sys.exit(2)


if __name__ == "__main__":
    main()
