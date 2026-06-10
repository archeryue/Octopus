"""End-to-end ClaudeCodeBackend tests against the real `claude` binary.

These cost real API calls (haiku, cheapest model). Auto-skipped when claude
isn't on PATH so CI without the binary still passes. Sits alongside
test_backend_claude_code.py (fake CLI) — the fake tests are kept as fast
regression checks for the wire-format parser; these prove the format is
actually correct.
"""

from __future__ import annotations

import asyncio
import os

import pytest

from server.harness import HarnessEvent, RunConfig, get_harness

# Widen PATH so shutil.which("claude") finds the binary even when the
# user's shell didn't export ~/.local/bin (typical for non-interactive
# pytest invocations). This is a no-op when claude is already on PATH.
_EXTRA_BIN_DIRS = [
    os.path.expanduser("~/.local/bin"),
    "/usr/local/bin",
]
for _d in _EXTRA_BIN_DIRS:
    if _d and _d not in os.environ.get("PATH", "").split(os.pathsep):
        os.environ["PATH"] = _d + os.pathsep + os.environ.get("PATH", "")


from tests.cli_gate import claude_cli_works

pytestmark = pytest.mark.skipif(
    not claude_cli_works(),
    reason="claude CLI unavailable or not signed in; skip real-CLI tests",
)

CWD = os.getcwd()


def _claude_run(**cfg):
    """A claude-code HarnessRun for the given RunConfig; the engine resolves
    the `claude` binary via the nvm-aware fallback."""
    return get_harness("claude-code").create_run(RunConfig(**cfg))


async def _drain(backend, timeout: float = 60.0) -> list[HarnessEvent]:
    events: list[HarnessEvent] = []

    async def collect() -> None:
        async for ev in backend.stream():
            events.append(ev)

    try:
        await asyncio.wait_for(collect(), timeout=timeout)
    except asyncio.TimeoutError:
        raise AssertionError(
            f"stream() didn't terminate within {timeout}s. "
            f"Collected so far: {[e.type for e in events]}"
        )
    return events


# Strings that, if seen in any tool_result or stderr, indicate our
# control-protocol payload was rejected by the CLI. The original bug: the
# backend sent the legacy can_use_tool control_response shape
# ({"allow": ...}) instead of the modern {"behavior": ...} one, which the
# CLI's strict validator rejected on AskUserQuestion. Fixed; this guard
# prevents a regression.
_CONTROL_PROTOCOL_RED_FLAGS = (
    "ZodError",
    "Tool permission request failed",
)


def _assert_no_control_protocol_errors(
    backend, events: list[HarnessEvent]
) -> None:
    """Fail loudly if the CLI logged a control-protocol error during this
    run, even if the model recovered (e.g. by retrying the tool call)."""
    for ev in events:
        if ev.type != "tool_result":
            continue
        content = ev.content or ""
        for flag in _CONTROL_PROTOCOL_RED_FLAGS:
            if flag in content:
                raise AssertionError(
                    f"CLI control protocol error in tool_result: {content[:300]!r}\n"
                    f"This means our backend is sending a payload the CLI's "
                    f"validator rejects (expected the modern "
                    f"{{'behavior': ...}} control_response shape)."
                )
    stderr = backend.stderr_text
    for flag in _CONTROL_PROTOCOL_RED_FLAGS:
        if flag in stderr:
            raise AssertionError(
                f"CLI control protocol error in stderr: {stderr[:300]!r}\n"
                f"Backend likely sent a control_response shape the CLI "
                f"validator rejects."
            )


# ---------------------------------------------------------------------------
# Basic happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_real_simple_text_then_result():
    backend = _claude_run(model="haiku")
    await backend.start("Reply with exactly: PONG", CWD)
    try:
        events = await _drain(backend, timeout=60.0)
    finally:
        await backend.stop()

    _assert_no_control_protocol_errors(backend, events)

    types = [e.type for e in events]
    # Always: at least one text + a result. Thinking blocks may or may not appear.
    assert "result" in types
    assert "text" in types
    text_concat = "".join(e.content or "" for e in events if e.type == "text")
    assert "PONG" in text_concat
    # Result carries cost, duration, and the session id we can resume on.
    result = next(e for e in events if e.type == "result")
    assert result.session_id is not None
    assert result.cost is not None and result.cost >= 0.0
    assert result.duration_ms is not None and result.duration_ms > 0


# ---------------------------------------------------------------------------
# Tool use chain
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_real_tool_use_then_tool_result():
    backend = _claude_run(model="haiku")
    await backend.start(
        "Use the Bash tool to run this exact command: echo PONG_FROM_BASH. "
        "Then say 'done' and stop.",
        CWD,
    )
    try:
        events = await _drain(backend, timeout=120.0)
    finally:
        await backend.stop()

    _assert_no_control_protocol_errors(backend, events)

    types = [e.type for e in events]
    # Expect at least one tool_use + matching tool_result + text + result
    tool_use = next((e for e in events if e.type == "tool_use"), None)
    assert tool_use is not None, f"no tool_use event; saw {types}"
    assert tool_use.tool_name == "Bash"
    assert "command" in (tool_use.tool_input or {})

    # The tool_result echo should match by tool_use_id and contain the output
    tr = next(
        (e for e in events if e.type == "tool_result" and e.tool_use_id == tool_use.tool_use_id),
        None,
    )
    assert tr is not None, "no matching tool_result"
    assert tr.is_error is False
    assert "PONG_FROM_BASH" in (tr.content or "")


# ---------------------------------------------------------------------------
# AskUserQuestion
# ---------------------------------------------------------------------------
#
# Under the VM0-shape backend (--dangerously-skip-permissions + the
# built-in AUQ disabled via --disallowedTools), AUQ no longer flows
# through the CLI control protocol. The model uses the
# `mcp__ask__user` MCP tool instead, which calls back into the
# Octopus FastAPI process over HTTP. Verifying that round-trip needs
# a live uvicorn + REST + frontend submission; it's covered by the
# Playwright e2e (`web/e2e/new-features.spec.ts`), not by a backend
# unit test. Keeping a unit test here would just mock the entire
# host out and prove nothing.


# ---------------------------------------------------------------------------
# Multi-turn resume
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_real_resume_across_two_subprocesses():
    """Turn 1 plants a fact; turn 2 spawns a fresh backend with --resume
    and proves it still has the context."""

    # Turn 1
    b1 = _claude_run(model="haiku")
    await b1.start(
        "Remember this exact word for later: MARIGOLD. Reply only with OK.",
        CWD,
    )
    try:
        e1 = await _drain(b1, timeout=60.0)
    finally:
        await b1.stop()

    _assert_no_control_protocol_errors(b1, e1)

    result1 = next(e for e in e1 if e.type == "result")
    sid = result1.session_id
    assert sid, "turn 1 didn't yield a resumable session id"

    # Turn 2 — fresh backend, --resume
    b2 = _claude_run(model="haiku")
    await b2.start(
        "What was the exact word I asked you to remember? Reply only with that word.",
        CWD,
        resume_id=sid,
    )
    try:
        e2 = await _drain(b2, timeout=60.0)
    finally:
        await b2.stop()

    _assert_no_control_protocol_errors(b2, e2)

    text2 = "".join(e.content or "" for e in e2 if e.type == "text")
    assert "MARIGOLD" in text2.upper(), (
        f"resumed turn didn't recall the planted word. Got: {text2!r}"
    )


# ---------------------------------------------------------------------------
# Interrupt control_request
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_real_interrupt_terminates_in_flight_turn():
    """Send a prompt that would take a while; immediately interrupt; the
    stream should terminate without hanging."""

    backend = _claude_run(model="haiku")
    # Prompt that's likely to take multiple turns / tool uses so we have
    # time to interrupt before result lands.
    await backend.start(
        "Write a 2000-word essay on the history of paper clips, "
        "broken into 10 sections, with citations for each.",
        CWD,
    )
    events: list[HarnessEvent] = []

    async def consume() -> None:
        async for ev in backend.stream():
            events.append(ev)

    consumer = asyncio.create_task(consume())

    # Give the model a moment to start streaming
    await asyncio.sleep(2.0)

    # Interrupt — should send the control_request and not hang
    await asyncio.wait_for(backend.interrupt(), timeout=10.0)

    # Consumer should wrap up promptly after interrupt + stop
    try:
        await asyncio.wait_for(consumer, timeout=15.0)
    except asyncio.TimeoutError:
        consumer.cancel()
        raise AssertionError(
            f"stream() didn't terminate after interrupt; collected {len(events)} events"
        )

    # We don't assert on event types — interrupt may land before or after
    # the model emits anything. The success criterion is: it didn't hang.
    _assert_no_control_protocol_errors(backend, events)


# ---------------------------------------------------------------------------
# Credential override (real API)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_real_credential_with_bad_key_yields_auth_error():
    """A credential with an obviously-invalid API key must override the
    default OAuth and cause the CLI to surface an auth error in its result.
    Proves the env-var injection actually takes effect at the CLI."""
    from server.harness import HarnessCredential

    backend = _claude_run(model="haiku")
    bad_cred = HarnessCredential(
        backend="claude-code",
        auth_type="api_key",
        secret="sk-ant-bogus-key-octopus-test",
    )
    await backend.start("Reply with: HI", CWD, credential=bad_cred)
    try:
        events = await _drain(backend, timeout=60.0)
    finally:
        await backend.stop()

    # Even on the failing auth path, we should not be tripping control
    # protocol validation — those are independent failure modes.
    _assert_no_control_protocol_errors(backend, events)

    # The CLI should report an error result (it can manifest as either an
    # error result or as a stderr-only failure; in either case, no normal
    # successful text response is expected).
    result = next((e for e in events if e.type == "result"), None)
    if result is not None:
        assert result.is_error is True, (
            f"bad credential should have failed but got success result: {result.raw}"
        )
    else:
        # No result event means the subprocess died before completing —
        # that's also a valid auth-failure signal.
        assert any(e.type == "error" for e in events) or not any(
            e.type == "text" for e in events
        ), f"bad credential produced unexpected normal flow: {[e.type for e in events]}"
