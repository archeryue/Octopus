#!/usr/bin/env python3
"""Fake `claude` CLI for ClaudeCodeBackend tests.

Reads the SDK-style control protocol from stdin (initialize, interrupt,
can_use_tool response) and emits scripted JSONL events on stdout based on
the mode passed as the first argv item.

Modes (each is one self-contained turn):
  hello              : initialize-ack, then simple text + result
  tool-success       : initialize-ack, then assistant tool_use + user tool_result + text + result
  ask-user-question  : initialize-ack, then assistant tool_use(AskUserQuestion), then a
                       can_use_tool control_request via stdin (wait for response),
                       then continue with text + result reflecting the answer.
  interrupt-respond  : initialize-ack, then wait forever; if it gets an interrupt control_request,
                       respond, emit a "result" with subtype=error_during_execution, exit.
"""

import json
import os
import sys
import time


def _emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _read_line():
    line = sys.stdin.readline()
    if not line:
        return None
    return json.loads(line)


def _await_control_response(request_id, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        line = _read_line()
        if line is None:
            return None
        if line.get("type") == "control_response":
            resp = line.get("response", {})
            if resp.get("request_id") == request_id:
                return resp
    return None


def _send_control_request(request_id, request):
    _emit({"type": "control_request", "request_id": request_id, "request": request})


def _wait_initialize_ack():
    """The backend's first message is an initialize control_request.

    Echo back a success response.
    """
    msg = _read_line()
    assert msg and msg.get("type") == "control_request"
    req = msg.get("request", {})
    assert req.get("subtype") == "initialize", f"expected initialize, got {req}"
    _emit({
        "type": "control_response",
        "response": {
            "subtype": "success",
            "request_id": msg["request_id"],
            "response": {"commands": [], "output_style": "default"},
        },
    })


def _emit_init(session_id="11111111-1111-1111-1111-111111111111"):
    _emit({
        "type": "system",
        "subtype": "init",
        "cwd": os.getcwd(),
        "session_id": session_id,
        "tools": ["Bash", "AskUserQuestion", "Read"],
        "model": "claude-haiku-4-5-20251001",
        "permissionMode": "default",
    })


def _emit_result(session_id="11111111-1111-1111-1111-111111111111", cost=0.001, subtype="success"):
    _emit({
        "type": "result",
        "subtype": subtype,
        "is_error": subtype != "success",
        "duration_ms": 100,
        "num_turns": 1,
        "session_id": session_id,
        "total_cost_usd": cost,
    })


def _read_user_turn():
    msg = _read_line()
    assert msg and msg.get("type") == "user", f"expected user turn, got {msg}"


def run_hello():
    _wait_initialize_ack()
    _emit_init()
    _read_user_turn()
    _emit({
        "type": "assistant",
        "message": {
            "model": "claude-haiku-4-5-20251001",
            "id": "msg_test_1",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": "Hello back."}],
        },
        "session_id": "11111111-1111-1111-1111-111111111111",
    })
    _emit_result()


def run_tool_success():
    _wait_initialize_ack()
    _emit_init()
    _read_user_turn()
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


def run_ask_user_question():
    _wait_initialize_ack()
    _emit_init()
    _read_user_turn()
    # Send a can_use_tool control_request for AskUserQuestion. The backend
    # is supposed to emit a question_request event upstream and hold the
    # request open until the host calls answer_question(); when it does,
    # it sends back a control_response that we (the fake CLI) read here.
    request_id = "req_ask_1"
    _send_control_request(
        request_id,
        {
            "subtype": "can_use_tool",
            "tool_name": "AskUserQuestion",
            "input": {
                "questions": [
                    {
                        "question": "Pick a color",
                        "options": [{"label": "red"}, {"label": "blue"}],
                    }
                ]
            },
            "permission_suggestions": [],
            "blocked_path": None,
        },
    )
    resp = _await_control_response(request_id, timeout=5.0)
    if resp is None:
        # Timeout — emit an error result
        _emit_result(subtype="error_during_execution")
        return
    response_body = resp.get("response", {}) or {}
    answer = response_body.get("reason", "")
    # Now emit a synthetic tool_result reflecting the answer, then text + result.
    _emit({
        "type": "user",
        "message": {
            "role": "user",
            "content": [{
                "type": "tool_result",
                "tool_use_id": "tool_q",
                "content": answer,
                "is_error": False,
            }],
        },
    })
    _emit({
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": f"User chose: {answer}"}],
        },
    })
    _emit_result()


def run_interrupt_respond():
    _wait_initialize_ack()
    _emit_init()
    _read_user_turn()
    # Now block reading stdin, waiting for an interrupt control_request.
    while True:
        line = _read_line()
        if line is None:
            return
        if line.get("type") == "control_request":
            req = line.get("request", {})
            if req.get("subtype") == "interrupt":
                _emit({
                    "type": "control_response",
                    "response": {
                        "subtype": "success",
                        "request_id": line["request_id"],
                        "response": {},
                    },
                })
                _emit_result(subtype="error_during_execution")
                return


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "hello"
    if mode == "hello":
        run_hello()
    elif mode == "tool-success":
        run_tool_success()
    elif mode == "ask-user-question":
        run_ask_user_question()
    elif mode == "interrupt-respond":
        run_interrupt_respond()
    else:
        sys.stderr.write(f"unknown mode: {mode}\n")
        sys.exit(2)


if __name__ == "__main__":
    main()
