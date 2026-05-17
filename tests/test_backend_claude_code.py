"""Unit tests for ClaudeCodeBackend against a scripted fake CLI.

Doesn't require the real `claude` binary; uses tests/_fixtures/fake_claude_cli.py.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest

from server.backends import BackendEvent, ClaudeCodeBackend

FAKE_CLI = Path(__file__).parent / "_fixtures" / "fake_claude_cli.py"


class _ScriptedClaudeCodeBackend(ClaudeCodeBackend):
    """ClaudeCodeBackend that runs our fake CLI in a chosen mode."""

    def __init__(self, mode: str, **kwargs):
        super().__init__(**kwargs)
        self._mode = mode

    def build_args(self, prompt, working_dir, resume_id, credential=None):
        # We deliberately do NOT use the real claude binary or its flags.
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
# Basic flow
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_simple_hello_emits_text_then_result(tmp_path):
    backend = _ScriptedClaudeCodeBackend("hello")
    await backend.start("hi", str(tmp_path))

    events = await asyncio.wait_for(_drain(backend.stream()), timeout=5.0)
    await backend.stop()

    assert [e.type for e in events] == ["text", "result"]
    assert events[0].content == "Hello back."
    assert events[1].session_id == "11111111-1111-1111-1111-111111111111"
    assert events[1].cost == 0.001


@pytest.mark.asyncio
async def test_tool_use_then_tool_result_then_text(tmp_path):
    backend = _ScriptedClaudeCodeBackend("tool-success")
    await backend.start("run the bash command", str(tmp_path))

    events = await asyncio.wait_for(_drain(backend.stream()), timeout=5.0)
    await backend.stop()

    types = [e.type for e in events]
    assert types == ["tool_use", "tool_result", "text", "result"]
    tu = events[0]
    assert tu.tool_name == "Bash"
    assert tu.tool_input == {"command": "echo hi"}
    assert tu.tool_use_id == "toolu_xyz"
    tr = events[1]
    assert tr.tool_use_id == "toolu_xyz"
    assert tr.content == "hi"
    assert tr.is_error is False


# ---------------------------------------------------------------------------
# AskUserQuestion control protocol
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ask_user_question_round_trip(tmp_path):
    """The fake CLI sends a can_use_tool for AskUserQuestion. The backend
    should emit a question_request event, hold the control_request, and
    relay the host's answer when answer_question() is called."""

    backend = _ScriptedClaudeCodeBackend("ask-user-question")
    await backend.start("ask me something", str(tmp_path))

    events: list[BackendEvent] = []

    async def consume() -> None:
        async for ev in backend.stream():
            events.append(ev)

    consumer_task = asyncio.create_task(consume())

    # Wait for the question_request event to land
    question_id: str | None = None
    for _ in range(100):
        for ev in events:
            if ev.type == "question_request":
                question_id = ev.tool_use_id
                break
        if question_id:
            break
        await asyncio.sleep(0.02)

    assert question_id is not None, f"never saw question_request; events={events}"

    # Sanity: the question input got through
    qreq = next(e for e in events if e.type == "question_request")
    assert qreq.tool_input == {
        "questions": [
            {"question": "Pick a color", "options": [{"label": "red"}, {"label": "blue"}]}
        ]
    }

    # Answer it — backend sends a control_response to the fake CLI, which
    # then continues with text + result.
    ok = await backend.answer_question(question_id, "red")
    assert ok is True

    await asyncio.wait_for(consumer_task, timeout=5.0)
    await backend.stop()

    types = [e.type for e in events]
    # question_request → (host answer) → CLI continues → tool_result + text + result
    assert types == ["question_request", "tool_result", "text", "result"]
    assert events[1].content == "red"
    assert events[2].content == "User chose: red"


@pytest.mark.asyncio
async def test_answer_question_unknown_returns_false(tmp_path):
    backend = _ScriptedClaudeCodeBackend("hello")
    await backend.start("hi", str(tmp_path))
    # Drain to completion
    async for _ in backend.stream():
        pass
    assert await backend.answer_question("nonexistent-q", "x") is False
    await backend.stop()


# ---------------------------------------------------------------------------
# Interrupt control protocol
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_interrupt_sends_control_request(tmp_path):
    backend = _ScriptedClaudeCodeBackend("interrupt-respond")
    await backend.start("start work", str(tmp_path))

    events: list[BackendEvent] = []

    async def consume() -> None:
        async for ev in backend.stream():
            events.append(ev)

    consumer_task = asyncio.create_task(consume())

    # Give the CLI a moment to be sitting on stdin
    await asyncio.sleep(0.1)

    # interrupt() sends the control_request then stops; the fake CLI
    # responds and emits an error-during-execution result.
    await asyncio.wait_for(backend.interrupt(), timeout=5.0)
    await asyncio.wait_for(consumer_task, timeout=5.0)

    types = [e.type for e in events]
    # We may receive zero or one "result" depending on timing — but if
    # we do, it should be flagged as an error.
    if "result" in types:
        result = next(e for e in events if e.type == "result")
        assert result.is_error is True


# ---------------------------------------------------------------------------
# Permission callback for non-AskUserQuestion tools (Phase 1d note: not
# exercised by the fake CLI; this is a unit test of the callback wiring.)
# ---------------------------------------------------------------------------


def test_build_args_injects_api_key_credential():
    from server.backends import BackendCredential, ClaudeCodeBackend

    backend = ClaudeCodeBackend()
    cred = BackendCredential(
        backend="claude-code", auth_type="api_key", secret="sk-test-123"
    )
    _argv, kwargs = backend.build_args("p", "/tmp", None, credential=cred)
    env = kwargs.get("env", {})
    assert env.get("ANTHROPIC_API_KEY") == "sk-test-123"


def test_build_args_no_credential_leaves_env_unchanged():
    from server.backends import ClaudeCodeBackend

    backend = ClaudeCodeBackend()
    _argv, kwargs = backend.build_args("p", "/tmp", None)
    env = kwargs.get("env", {})
    # ANTHROPIC_API_KEY may or may not be set by the host shell — we only
    # care that we didn't *override* it from a None credential.
    import os as _os
    assert env.get("ANTHROPIC_API_KEY") == _os.environ.get("ANTHROPIC_API_KEY")


@pytest.mark.asyncio
async def test_permission_callback_invoked_directly():
    """Construct the backend with a callback; call the internal handler
    directly to verify the wiring (no subprocess involved). Asserts the
    on-wire payload uses the new behavior/updatedInput shape — sending the
    legacy {"allow": true} would be rejected by the CLI with a ZodError.
    """
    seen: list[tuple[str, dict[str, Any]]] = []

    async def cb(tool_name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
        seen.append((tool_name, tool_input))
        return {"behavior": "allow", "updatedInput": tool_input}

    backend = ClaudeCodeBackend(permission_callback=cb)

    # Wire a fake stdin so _write_stdin doesn't blow up. Easiest: monkeypatch.
    sent: list[str] = []

    async def fake_write(payload: str) -> None:
        sent.append(payload)

    backend._write_stdin = fake_write  # type: ignore[method-assign]

    await backend._handle_control_request(
        {
            "type": "control_request",
            "request_id": "req_x",
            "request": {
                "subtype": "can_use_tool",
                "tool_name": "Bash",
                "input": {"command": "ls"},
            },
        }
    )

    assert seen == [("Bash", {"command": "ls"})]
    # New shape: {"behavior": "allow", "updatedInput": {"command": "ls"}}
    assert any('"behavior": "allow"' in s for s in sent)
    assert any('"updatedInput"' in s for s in sent)
    # The legacy shape must NOT appear — that's what triggered ZodError.
    assert not any('"allow": true' in s for s in sent)


@pytest.mark.asyncio
async def test_handler_default_allow_uses_new_shape():
    """No callback set → default allow path. Verify it sends the new shape
    with updatedInput (the original tool input passed through), not the
    legacy {"allow": true}."""
    backend = ClaudeCodeBackend()
    sent: list[str] = []

    async def fake_write(payload: str) -> None:
        sent.append(payload)

    backend._write_stdin = fake_write  # type: ignore[method-assign]

    await backend._handle_control_request(
        {
            "type": "control_request",
            "request_id": "req_y",
            "request": {
                "subtype": "can_use_tool",
                "tool_name": "Bash",
                "input": {"command": "pwd"},
            },
        }
    )

    assert any('"behavior": "allow"' in s for s in sent)
    assert any('"updatedInput"' in s and "pwd" in s for s in sent)


@pytest.mark.asyncio
async def test_answer_question_sends_deny_shape():
    """The user's AskUserQuestion answer must travel back as
    {"behavior": "deny", "message": ...}, not the legacy
    {"allow": false, "reason": ...} that triggered ZodError."""
    backend = ClaudeCodeBackend()
    sent: list[str] = []

    async def fake_write(payload: str) -> None:
        sent.append(payload)

    backend._write_stdin = fake_write  # type: ignore[method-assign]

    # Seed a pending question as if the CLI had already asked.
    backend._pending_incoming["req_q1"] = {"subtype": "can_use_tool"}
    backend._question_to_request["qid1"] = "req_q1"

    ok = await backend.answer_question("qid1", "User said red")
    assert ok is True

    assert any('"behavior": "deny"' in s for s in sent)
    assert any('"message": "User said red"' in s for s in sent)
    assert not any('"reason"' in s for s in sent)
