"""Shared driver: spawn a CLI, read JSONL stdout, write to stdin, normalize.

Concrete backends subclass this and implement `build_args`, `on_stdout_line`,
and (optionally) `send_initial_prompt`. Lifecycle and I/O are shared.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from abc import abstractmethod
from collections.abc import AsyncIterator
from typing import Any

from .base import BackendBase, BackendCredential, BackendEvent

logger = logging.getLogger(__name__)

# Sentinel pushed onto the event queue to signal EOF on the stdout reader.
_STREAM_END = object()

# Per-line buffer cap for the asyncio StreamReader wrapping the CLI's
# stdout. Default in asyncio is 64 KiB, which is *very* easy for the
# Claude CLI to exceed in a single stream-json event — e.g. a
# `user` event carrying a tool_result for a Read of a 100 KB+ file
# produces a single 100 KB+ line. When the line exceeds the limit
# without a newline, asyncio raises LimitOverrunError → ValueError,
# which crashes the stdout reader and tears down the turn. Set this
# generously (4 MiB) so anything short of pathological emits cleanly;
# real backpressure on the pipe still keeps memory bounded.
_STDOUT_LINE_LIMIT_BYTES = 4 * 1024 * 1024


def _which_with_fallback(binary: str) -> str | None:
    """shutil.which, then retry against PATH + common per-user install dirs.

    systemd's default PATH excludes ~/.local/bin and node/npm global bins,
    so a CLI installed for the invoking user is invisible to the service
    unless we add those dirs ourselves.
    """
    found = shutil.which(binary)
    if found is not None:
        return found
    home = os.path.expanduser("~")
    extras = [
        os.path.join(home, ".local/bin"),
        os.path.join(home, ".npm-global/bin"),
        "/usr/local/bin",
        "/opt/homebrew/bin",
    ]
    extra_path = os.pathsep.join(extras)
    full_path = os.pathsep.join(p for p in (os.environ.get("PATH", ""), extra_path) if p)
    return shutil.which(binary, path=full_path)


class SubprocessJsonlBackend(BackendBase):
    """Common subprocess + JSONL machinery for both Claude Code and Codex.

    The subclass tells us how to spawn (`build_args`) and how to parse one
    line of stdout (`on_stdout_line` — uses `self._emit(event)` to push
    normalized events). Everything else — lifecycle, error recovery,
    stderr buffering, graceful shutdown — lives here.
    """

    binary: str = ""  # subclass overrides; resolved via shutil.which

    # ------------------------------------------------------------------ subclass hooks

    @abstractmethod
    def build_args(
        self,
        prompt: str,
        working_dir: str,
        resume_id: str | None,
        credential: BackendCredential | None = None,
    ) -> tuple[list[str], dict[str, Any]]:
        """Return (argv, kwargs) for asyncio.create_subprocess_exec.

        argv[0] is normally the resolved binary path. kwargs typically just
        sets `cwd`; environment overrides go in `env`. When a credential is
        provided the subclass typically materializes it into the env dict
        here (e.g. ANTHROPIC_API_KEY).
        """

    @abstractmethod
    async def on_stdout_line(self, line: str) -> None:
        """Parse one stdout JSON line and emit zero or more BackendEvents
        via `self._emit(event)`.
        """

    async def send_initial_prompt(self, prompt: str) -> None:
        """Subclass hook: write the user prompt to stdin (if applicable).

        Default: no-op (for backends that take the prompt as a CLI arg).
        """

    # ------------------------------------------------------------------ state

    def __init__(self) -> None:
        self._process: asyncio.subprocess.Process | None = None
        self._event_queue: asyncio.Queue[BackendEvent | object] = asyncio.Queue()
        self._stdout_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._stderr_lines: list[str] = []
        # Set when the stream has ended (normally or via stop()). Lets us
        # drop the second EOF sentinel if both happen.
        self._stream_closed: bool = False

    # ------------------------------------------------------------------ lifecycle

    async def start(
        self,
        prompt: str,
        working_dir: str,
        resume_id: str | None = None,
        credential: BackendCredential | None = None,
    ) -> None:
        if self._process is not None:
            raise RuntimeError(f"{type(self).__name__} already started")

        argv, kwargs = self.build_args(prompt, working_dir, resume_id, credential)
        # If the subclass used a bare binary name, resolve it. We search the
        # current PATH plus common per-user install dirs that systemd-style
        # launchers strip from PATH (npm global, pipx, asdf, etc.) so a user
        # who can run `claude` in their shell doesn't have to also configure
        # the service unit.
        if argv and not os.path.isabs(argv[0]):
            resolved = _which_with_fallback(argv[0])
            if resolved is None:
                raise FileNotFoundError(
                    f"{argv[0]} not found on PATH — install the CLI first"
                )
            argv = [resolved, *argv[1:]]

        logger.info("Spawning backend: %s (cwd=%s)", argv, kwargs.get("cwd"))
        self._process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # Raise the per-line buffer cap from asyncio's 64 KiB
            # default; otherwise a single oversized stream-json event
            # (typically a tool_result carrying a big Read output)
            # crashes the reader with LimitOverrunError. See
            # _STDOUT_LINE_LIMIT_BYTES above.
            limit=_STDOUT_LINE_LIMIT_BYTES,
            **kwargs,
        )
        self._stdout_task = asyncio.create_task(
            self._read_stdout(), name=f"{type(self).__name__}-stdout"
        )
        self._stderr_task = asyncio.create_task(
            self._read_stderr(), name=f"{type(self).__name__}-stderr"
        )

        try:
            await self.send_initial_prompt(prompt)
        except Exception:
            # If the subclass fails to send the prompt, kill the subprocess
            # so we don't leak it.
            logger.exception("send_initial_prompt failed, killing subprocess")
            await self.stop()
            raise

    async def stream(self) -> AsyncIterator[BackendEvent]:
        while True:
            item = await self._event_queue.get()
            if item is _STREAM_END:
                return
            assert isinstance(item, BackendEvent)
            yield item

    async def stop(self) -> None:
        """Terminate the subprocess, drain reader tasks. Idempotent."""
        proc = self._process
        if proc is None:
            return

        # Closing stdin lets the CLI exit gracefully (it'll flush its result
        # event first). If the subprocess is already gone, this is a no-op.
        if proc.stdin and not proc.stdin.is_closing():
            try:
                proc.stdin.close()
            except Exception:
                pass

        # Graceful → terminate → kill, each with its own timeout. The CLI
        # can occasionally take a moment to flush; don't camp on it forever.
        if proc.returncode is None:
            try:
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                logger.warning("CLI didn't exit on stdin close, terminating")
                try:
                    proc.terminate()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(proc.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    logger.warning("CLI didn't exit on SIGTERM, killing")
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
                    await proc.wait()

        # Drain the reader tasks. They'll wake up on EOF or get cancelled.
        for task in (self._stdout_task, self._stderr_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

        # Make sure the stream consumer wakes up if it's still iterating.
        if not self._stream_closed:
            self._stream_closed = True
            try:
                self._event_queue.put_nowait(_STREAM_END)
            except asyncio.QueueFull:
                pass

        self._process = None
        self._stdout_task = None
        self._stderr_task = None

    # ------------------------------------------------------------------ helpers for subclasses

    def _emit(self, event: BackendEvent) -> None:
        """Push a normalized event onto the stream queue."""
        if self._stream_closed:
            return
        self._event_queue.put_nowait(event)

    def _close_stream(self) -> None:
        """Signal end-of-stream to consumers of stream().

        Used by subclasses to terminate the iterator at a logical boundary
        (e.g., after a `result` event) before the subprocess actually exits.
        """
        if self._stream_closed:
            return
        self._stream_closed = True
        try:
            self._event_queue.put_nowait(_STREAM_END)
        except asyncio.QueueFull:
            pass

    async def _write_stdin(self, payload: str) -> None:
        """Write a string to the subprocess stdin (typically one JSONL line)."""
        if self._process is None or self._process.stdin is None:
            raise RuntimeError("Cannot write to stdin: backend not started")
        self._process.stdin.write(payload.encode())
        await self._process.stdin.drain()

    @property
    def stderr_text(self) -> str:
        """All stderr seen so far, joined with newlines. For error reporting."""
        return "\n".join(self._stderr_lines)

    @staticmethod
    def parse_json_line(line: str) -> dict[str, Any] | None:
        """Parse a JSONL line, returning None on parse error (logs a warning)."""
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as e:
            logger.warning("Skipping unparseable backend line: %s — %s", line[:200], e)
            return None
        if not isinstance(obj, dict):
            logger.warning("Unexpected non-object backend line: %s", line[:200])
            return None
        return obj

    # ------------------------------------------------------------------ reader tasks

    async def _read_stdout(self) -> None:
        assert self._process and self._process.stdout
        try:
            async for raw in self._process.stdout:
                line = raw.decode(errors="replace").rstrip("\r\n")
                if not line:
                    continue
                try:
                    await self.on_stdout_line(line)
                except Exception:
                    logger.exception(
                        "%s.on_stdout_line crashed on: %s",
                        type(self).__name__,
                        line[:200],
                    )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("stdout reader crashed")
        finally:
            # Signal end-of-stream to consumers (idempotent — stop() also pushes one).
            if not self._stream_closed:
                self._stream_closed = True
                try:
                    self._event_queue.put_nowait(_STREAM_END)
                except asyncio.QueueFull:
                    pass

    async def _read_stderr(self) -> None:
        assert self._process and self._process.stderr
        try:
            async for raw in self._process.stderr:
                line = raw.decode(errors="replace").rstrip("\r\n")
                if line:
                    self._stderr_lines.append(line)
                    logger.debug("CLI stderr: %s", line)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("stderr reader crashed")
