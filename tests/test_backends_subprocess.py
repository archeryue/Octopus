"""Tests for the shared SubprocessJsonlBackend driver, using a fake CLI script."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest

from server.backends import BackendEvent, SubprocessJsonlBackend

FAKE_CLI = Path(__file__).parent / "_fixtures" / "fake_cli.py"


class _ScriptedBackend(SubprocessJsonlBackend):
    """Backend that runs our fake CLI in the requested mode.

    `mode_args` is passed straight to fake_cli.py after the mode keyword.
    `on_stdout_line` parses each JSON object and emits a BackendEvent whose
    `raw` carries the parsed dict — that's enough to exercise the driver.
    """

    name = "scripted"

    def __init__(self, mode: str, *mode_args: str) -> None:
        super().__init__()
        self._mode = mode
        self._mode_args = list(mode_args)

    def build_args(
        self,
        prompt: str,
        working_dir: str,
        resume_id: str | None,
        credential=None,
    ) -> tuple[list[str], dict[str, Any]]:
        return (
            [sys.executable, str(FAKE_CLI), self._mode, *self._mode_args],
            {"cwd": working_dir},
        )

    async def on_stdout_line(self, line: str) -> None:
        obj = self.parse_json_line(line)
        if obj is None:
            return
        self._emit(BackendEvent(type=obj.get("type", "?"), raw=obj))


class _EchoBackend(_ScriptedBackend):
    """Echo-stdin mode: send the prompt to stdin and let the CLI echo it."""

    async def send_initial_prompt(self, prompt: str) -> None:
        await self._write_stdin(prompt + "\n")
        # Close stdin so the fake CLI knows we're done streaming.
        assert self._process and self._process.stdin
        self._process.stdin.close()


# ---------------------------------------------------------------------------
# Basic event streaming
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_emits_each_jsonl_line_as_event(tmp_path):
    backend = _ScriptedBackend(
        "emit-lines",
        '{"type":"hello"}',
        '{"type":"world","extra":1}',
    )
    await backend.start("prompt", str(tmp_path))

    events: list[BackendEvent] = []
    async for ev in backend.stream():
        events.append(ev)

    await backend.stop()

    assert [e.type for e in events] == ["hello", "world"]
    assert events[1].raw == {"type": "world", "extra": 1}


@pytest.mark.asyncio
async def test_skips_malformed_lines_continues(tmp_path):
    backend = _ScriptedBackend("bad-json")
    await backend.start("prompt", str(tmp_path))

    events = [e async for e in backend.stream()]
    await backend.stop()

    # First line was invalid JSON; second line is parseable. The driver
    # should swallow the parse error and keep going.
    assert [e.type for e in events] == ["good"]


@pytest.mark.asyncio
async def test_stream_ends_when_subprocess_exits(tmp_path):
    backend = _ScriptedBackend("emit-lines", '{"type":"only"}')
    await backend.start("p", str(tmp_path))

    # The iterator must terminate (not hang forever) once the subprocess
    # closes stdout. This is the property that lets the session manager
    # know "the turn is done."
    events = await asyncio.wait_for(
        _drain(backend.stream()), timeout=3.0
    )
    await backend.stop()
    assert [e.type for e in events] == ["only"]


async def _drain(stream) -> list[BackendEvent]:
    out: list[BackendEvent] = []
    async for ev in stream:
        out.append(ev)
    return out


# ---------------------------------------------------------------------------
# Stdin streaming
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_initial_prompt_writes_to_stdin(tmp_path):
    backend = _EchoBackend("echo-stdin")
    await backend.start("hello there", str(tmp_path))

    events = await asyncio.wait_for(_drain(backend.stream()), timeout=3.0)
    await backend.stop()

    assert len(events) == 1
    assert events[0].raw == {"type": "echo", "content": "hello there"}


# ---------------------------------------------------------------------------
# Lifecycle / errors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_starting_twice_raises(tmp_path):
    backend = _ScriptedBackend("emit-lines", '{"type":"x"}')
    await backend.start("p", str(tmp_path))
    with pytest.raises(RuntimeError, match="already started"):
        await backend.start("p", str(tmp_path))
    await backend.stop()


@pytest.mark.asyncio
async def test_stop_is_idempotent(tmp_path):
    backend = _ScriptedBackend("emit-lines", '{"type":"x"}')
    await backend.start("p", str(tmp_path))
    async for _ in backend.stream():
        pass
    await backend.stop()
    # Second stop() is a no-op, not an error
    await backend.stop()


@pytest.mark.asyncio
async def test_missing_binary_raises(tmp_path):
    class _Missing(_ScriptedBackend):
        def build_args(self, prompt, working_dir, resume_id, credential=None):
            return (["definitely-not-a-real-binary-12345"], {"cwd": working_dir})

    backend = _Missing("emit-lines")
    with pytest.raises(FileNotFoundError, match="not found on PATH"):
        await backend.start("p", str(tmp_path))


@pytest.mark.asyncio
async def test_stderr_is_captured(tmp_path):
    backend = _ScriptedBackend("fail-exit")
    await backend.start("p", str(tmp_path))
    # Drain stream so the subprocess can exit cleanly
    async for _ in backend.stream():
        pass
    await backend.stop()
    # The fake-cli writes "boom" to stderr. We capture all of it.
    assert "boom" in backend.stderr_text


@pytest.mark.asyncio
async def test_stop_kills_hung_subprocess(tmp_path):
    """If the subprocess won't exit on stdin close, stop() must still return."""
    # sleep-then sleeps 30s, ignoring stdin close. stop() should escalate
    # from close-stdin → terminate → kill, with each tier timing out fast.
    backend = _ScriptedBackend("sleep-then", "30")
    await backend.start("p", str(tmp_path))

    # Hand off stop() with a wall-clock budget — total escalation ~4s.
    await asyncio.wait_for(backend.stop(), timeout=6.0)
