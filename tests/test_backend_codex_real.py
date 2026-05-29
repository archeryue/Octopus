"""End-to-end CodexBackend tests against the real `codex` binary.

These need `codex` installed AND a logged-in ChatGPT subscription
(`~/.codex/auth.json`). Auto-skipped otherwise so CI without a login still
passes. They sit alongside test_backend_codex.py (fake CLI): the fake tests
are fast regression checks for the normalizer; these prove the wire format is
actually what codex emits (codex-backend.md §12, Phase C — keeps the fake
fixture honest against version drift).

Confirmed against codex 0.132.0 in this session:
  thread.started{thread_id} / turn.started / item.completed{agent_message}
  / item.{started,completed}{command_execution} / item.{started,completed}
  {mcp_tool_call: server,tool,arguments,result} / turn.completed{usage}.
"""

from __future__ import annotations

import asyncio
import os

import pytest

from server.harness import HarnessEvent, RunConfig, get_harness
from server.harness.run import _which_with_fallback

_codex = _which_with_fallback("codex")
_logged_in = os.path.exists(os.path.expanduser("~/.codex/auth.json"))

pytestmark = pytest.mark.skipif(
    _codex is None or not _logged_in,
    reason="codex CLI not on PATH or no ~/.codex login; skipping real-CLI tests",
)


def _codex_run(**cfg):
    """A codex HarnessRun for the given RunConfig. The engine resolves the
    `codex` binary via the same nvm-aware fallback, so this spawns even when
    codex lives only under ~/.nvm/.../bin (not on the service PATH)."""
    return get_harness("codex").create_run(RunConfig(**cfg))


async def _drain(backend, timeout: float = 150.0) -> list[HarnessEvent]:
    events: list[HarnessEvent] = []

    async def collect() -> None:
        async for ev in backend.stream():
            events.append(ev)

    try:
        await asyncio.wait_for(collect(), timeout=timeout)
    except asyncio.TimeoutError:
        raise AssertionError(
            f"stream() didn't terminate within {timeout}s. "
            f"Collected: {[e.type for e in events]}"
        )
    return events


@pytest.mark.asyncio
async def test_real_text_then_result(tmp_path):
    backend = _codex_run(session_id="rt", mcp_servers=[])
    await backend.start("Reply with exactly: PONG. Do not use any tools.", str(tmp_path))
    try:
        events = await _drain(backend)
    finally:
        await backend.stop()

    types = [e.type for e in events]
    assert types[0] == "session_started"
    assert "text" in types and "result" in types
    text = "".join(e.content or "" for e in events if e.type == "text")
    assert "PONG" in text
    result = next(e for e in events if e.type == "result")
    # thread_id is the resume handle; Codex reports tokens, not USD.
    assert result.session_id is not None
    assert result.cost is None
    assert result.is_error is False


@pytest.mark.asyncio
async def test_real_command_execution(tmp_path):
    backend = _codex_run(session_id="rc", mcp_servers=[])
    await backend.start(
        "Use the shell to run exactly: echo PONG_FROM_CODEX. Then reply: done.",
        str(tmp_path),
    )
    try:
        events = await _drain(backend)
    finally:
        await backend.stop()

    tu = next((e for e in events if e.type == "tool_use"), None)
    assert tu is not None, f"no tool_use; saw {[e.type for e in events]}"
    assert tu.tool_name == "Bash"
    assert "command" in (tu.tool_input or {})
    tr = next(
        (e for e in events if e.type == "tool_result" and e.tool_use_id == tu.tool_use_id),
        None,
    )
    assert tr is not None and tr.is_error is False
    assert "PONG_FROM_CODEX" in (tr.content or "")


@pytest.mark.asyncio
async def test_real_resume_across_two_subprocesses(tmp_path):
    b1 = _codex_run(session_id="r1", mcp_servers=[])
    await b1.start(
        "Remember this exact word for later: MARIGOLD. Reply only with OK.",
        str(tmp_path),
    )
    try:
        e1 = await _drain(b1)
    finally:
        await b1.stop()
    sid = next(e for e in e1 if e.type == "result").session_id
    assert sid, "turn 1 didn't yield a resumable thread id"

    b2 = _codex_run(session_id="r2", mcp_servers=[])
    await b2.start(
        "What exact word did I ask you to remember? Reply only with that word.",
        str(tmp_path),
        resume_id=sid,
    )
    try:
        e2 = await _drain(b2)
    finally:
        await b2.stop()
    text = "".join(e.content or "" for e in e2 if e.type == "text")
    assert "MARIGOLD" in text.upper(), f"resume didn't recall the word; got {text!r}"
