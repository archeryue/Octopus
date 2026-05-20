#!/usr/bin/env python3
"""Fake `codex exec --json` CLI for CodexBackend tests.

Emits a scripted `codex exec --json` event stream on stdout and exits — no
stdin protocol. The event shapes are transcribed from VM0's shipped Codex
parser (`turbo/apps/cli/.../codex-event-parser.ts`) and the codex-backend.md
§5.2 mapping. The prompt (positional argv after `--`) is ignored; the mode
arg drives the output.

Invocation:
    python fake_codex_cli.py <mode> [codex exec flags...] [-- <prompt>]

Modes:
  hello : thread.started + agent_message + turn.completed.
  tool  : thread.started + command_execution (started/completed) +
          agent_message + turn.completed.
  files : thread.started + file_write (started/completed) + file_change +
          turn.completed.
  failed: thread.started + turn.failed.
  error : a bare error event (no thread).
"""

import json
import sys

THREAD_ID = "thr_00000000000000000000000000"


def _emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _thread_started():
    _emit({"type": "thread.started", "thread_id": THREAD_ID})


def _turn_completed():
    _emit(
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 12, "cached_input_tokens": 0, "output_tokens": 7},
        }
    )


def run_hello():
    _thread_started()
    _emit({"type": "turn.started"})
    _emit(
        {
            "type": "item.completed",
            "item": {"id": "i1", "type": "agent_message", "text": "Hello from Codex."},
        }
    )
    _turn_completed()


def run_tool():
    _thread_started()
    _emit(
        {
            "type": "item.started",
            "item": {
                "id": "c1",
                "type": "command_execution",
                "command": "echo hi",
                "status": "in_progress",
            },
        }
    )
    _emit(
        {
            "type": "item.completed",
            "item": {
                "id": "c1",
                "type": "command_execution",
                "command": "echo hi",
                "exit_code": 0,
                "aggregated_output": "hi\n",
            },
        }
    )
    _emit(
        {
            "type": "item.completed",
            "item": {"id": "m1", "type": "agent_message", "text": "Ran it."},
        }
    )
    _turn_completed()


def run_files():
    _thread_started()
    _emit(
        {
            "type": "item.started",
            "item": {"id": "f1", "type": "file_write", "path": "/tmp/out.txt"},
        }
    )
    _emit(
        {
            "type": "item.completed",
            "item": {
                "id": "f1",
                "type": "file_write",
                "path": "/tmp/out.txt",
                "diff": "+hello",
            },
        }
    )
    _emit(
        {
            "type": "item.completed",
            "item": {
                "id": "fc1",
                "type": "file_change",
                "changes": [{"kind": "add", "path": "/tmp/out.txt"}],
            },
        }
    )
    _turn_completed()


def run_mcp():
    # Exact mcp_tool_call shape captured from real codex 0.132.0 (Phase C).
    _thread_started()
    _emit(
        {
            "type": "item.started",
            "item": {
                "id": "t1",
                "type": "mcp_tool_call",
                "server": "viewer",
                "tool": "show_file",
                "arguments": {"path": "hello.txt"},
                "result": None,
                "error": None,
                "status": "in_progress",
            },
        }
    )
    _emit(
        {
            "type": "item.completed",
            "item": {
                "id": "t1",
                "type": "mcp_tool_call",
                "server": "viewer",
                "tool": "show_file",
                "arguments": {"path": "hello.txt"},
                "result": {
                    "content": [{"type": "text", "text": "Opened hello.txt (text, 14 bytes)."}],
                    "structured_content": {"result": "Opened hello.txt (text, 14 bytes)."},
                },
                "error": None,
                "status": "completed",
            },
        }
    )
    _emit(
        {
            "type": "item.completed",
            "item": {"id": "m1", "type": "agent_message", "text": "done"},
        }
    )
    _turn_completed()


def run_failed():
    _thread_started()
    _emit({"type": "turn.failed", "error": "model refused"})


def run_error():
    _emit({"type": "error", "message": "unrecoverable codex error"})


def main():
    # Mirror real `codex exec`: it reads stdin (appending it as a `<stdin>`
    # block) and blocks until EOF even with a positional prompt. CodexBackend
    # closes stdin on spawn so this returns immediately — if that fix ever
    # regresses, the backend tests hang here instead of only the real-CLI
    # suite catching it.
    try:
        sys.stdin.read()
    except Exception:
        pass

    mode = sys.argv[1] if len(sys.argv) > 1 else "hello"
    {
        "hello": run_hello,
        "tool": run_tool,
        "files": run_files,
        "mcp": run_mcp,
        "failed": run_failed,
        "error": run_error,
    }.get(mode, lambda: sys.exit(2))()


if __name__ == "__main__":
    main()
