"""`HarnessRun` — the single subprocess + JSONL streaming engine.

One concrete class for both harnesses (no per-framework subclasses). It
owns the subprocess lifecycle, stdout/stderr readers, the normalized
event queue, and graceful shutdown — exactly the machinery that used to
live in `SubprocessJsonlBackend`. The two things that differ per harness
come from the `RuntimeProfile` it's constructed with: how to build argv
(`profile.build_turn_argv`) and how to normalize a stdout line
(`profile.new_event_parser()`), plus whether to close stdin after spawn.
"""

from __future__ import annotations

import asyncio
import glob
import json
import logging
import os
import shutil
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import assembly
from .events import HarnessCredential, HarnessEvent
from .profile import RuntimeProfile, TurnContext

logger = logging.getLogger(__name__)

# Sentinel pushed onto the event queue to signal EOF on the stdout reader.
_STREAM_END = object()

# Per-line buffer cap for the asyncio StreamReader wrapping the CLI's
# stdout. asyncio's 64 KiB default is easily exceeded by a single
# stream-json event (e.g. a tool_result carrying a big Read output);
# overrun raises LimitOverrunError and crashes the reader. 4 MiB lets
# anything short of pathological emit cleanly; pipe backpressure keeps
# memory bounded.
_STDOUT_LINE_LIMIT_BYTES = 4 * 1024 * 1024


def _fallback_path_dirs() -> list[str]:
    """Per-user install dirs a systemd-style service PATH typically strips:
    ~/.local/bin, npm-global, Homebrew, and every nvm node version's bin."""
    home = os.path.expanduser("~")
    extras = [
        os.path.join(home, ".local/bin"),
        os.path.join(home, ".npm-global/bin"),
        "/usr/local/bin",
        "/opt/homebrew/bin",
    ]
    extras += sorted(glob.glob(os.path.join(home, ".nvm/versions/node/*/bin")))
    return extras


def _which_with_fallback(binary: str) -> str | None:
    """shutil.which, then retry against PATH + common per-user install dirs.

    systemd's default PATH excludes ~/.local/bin and node/npm global bins,
    so a CLI installed for the invoking user is invisible to the service
    unless we add those dirs ourselves."""
    found = shutil.which(binary)
    if found is not None:
        return found
    extra_path = os.pathsep.join(_fallback_path_dirs())
    full_path = os.pathsep.join(p for p in (os.environ.get("PATH", ""), extra_path) if p)
    return shutil.which(binary, path=full_path)


def augmented_path(base: str | None = None, extra_dir: str | None = None) -> str:
    """A PATH that includes the per-user install dirs (and optionally the
    resolved CLI's own dir) ahead of the base PATH. Critical for node-based
    CLIs: `claude`/`codex` are `#!/usr/bin/env node` scripts, so the child
    must find `node` at exec time; the service PATH usually omits the nvm
    bin where node lives (else: exit 127)."""
    if base is None:
        base = os.environ.get("PATH", "")
    dirs = ([extra_dir] if extra_dir else []) + _fallback_path_dirs()
    return os.pathsep.join([d for d in dirs if d] + ([base] if base else []))


def prepare_spawn(
    argv: list[str], kwargs: dict[str, Any]
) -> tuple[list[str], dict[str, Any]]:
    """Resolve a bare binary name to an absolute path (PATH + per-user
    fallback dirs) and augment the child's PATH so its node shebang resolves.
    Shared by the streaming engine and `Harness.run_oneshot`."""
    if argv and not os.path.isabs(argv[0]):
        resolved = _which_with_fallback(argv[0])
        if resolved is None:
            raise FileNotFoundError(
                f"{argv[0]} not found on PATH — install the CLI first"
            )
        argv = [resolved, *argv[1:]]
    env = kwargs.get("env") or os.environ.copy()
    cli_dir = os.path.dirname(argv[0]) if argv and os.path.isabs(argv[0]) else None
    env["PATH"] = augmented_path(env.get("PATH"), cli_dir)
    return argv, {**kwargs, "env": env}


def parse_json_line(line: str) -> dict[str, Any] | None:
    """Parse a JSONL line, returning None on parse error (logs a warning)."""
    try:
        obj = json.loads(line)
    except json.JSONDecodeError as e:
        logger.warning("Skipping unparseable harness line: %s — %s", line[:200], e)
        return None
    if not isinstance(obj, dict):
        logger.warning("Unexpected non-object harness line: %s", line[:200])
        return None
    return obj


@dataclass
class RunConfig:
    """Agent-derived per-run configuration (resolved fresh each turn by
    session_manager). Distinct from the start() args (prompt/working_dir/
    resume_id/credential), which arrive per invocation."""

    session_id: str | None = None
    system_prompt: str | None = None   # agent persona
    model: str | None = None
    mcp_servers: list[str] | None = None
    tool_allow: list[str] | None = None
    tool_deny: list[str] | None = None
    connectors: list[tuple[Any, Any]] = field(default_factory=list)
    # Per-agent native memory (docs/plans/memory.md). None when there's no
    # owning agent (legacy/tests) → memory wiring is fully inert.
    memory_dir: str | None = None
    # Fork first-turn context note (session-tree-rewind.md §5.6.4): framing
    # appended to the system addendum on a fork's first turn only. None
    # otherwise. NOT the replay transcript (that lives in the user channel).
    fork_note: str | None = None


class HarnessRun:
    """One streamed turn. Lifecycle: `start()`, iterate `stream()` until a
    terminal event closes it, then `stop()`. `interrupt()` cancels in-flight."""

    def __init__(self, profile: RuntimeProfile, config: RunConfig | None = None) -> None:
        self._profile = profile
        self._config = config or RunConfig()
        self._parser = profile.new_event_parser()
        self._process: asyncio.subprocess.Process | None = None
        self._event_queue: asyncio.Queue[HarnessEvent | object] = asyncio.Queue()
        self._stdout_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._stderr_lines: list[str] = []
        self._stream_closed: bool = False

    @property
    def profile(self) -> RuntimeProfile:
        return self._profile

    # ------------------------------------------------------------------ lifecycle

    def _make_context(
        self,
        prompt: str,
        working_dir: str,
        resume_id: str | None,
        credential: HarnessCredential | None,
    ) -> TurnContext:
        """Run the shared assembly (MCP selection, system-prompt composition,
        working-dir absolutization) into a neutral TurnContext. Side-effect
        free, so both `build_argv` (argv inspection) and `start()` use it."""
        # Resolve working_dir to ABSOLUTE before handing it to the CLI: MCP
        # grandchildren inherit cwd, so a relative path would be double-resolved.
        abs_wd = str(Path(working_dir).resolve())
        callback_env = assembly.build_callback_env(self._config.session_id)
        mcp_servers = assembly.select_mcp_servers(
            self._config.mcp_servers, self._config.connectors, callback_env
        )
        system_prompt = assembly.compose_system_prompt(
            self._config.system_prompt,
            self._profile.tools_prompt,
            self._config.connectors,
            memory_dir=self._config.memory_dir,
            inject_memory=self._profile.injects_memory_prompt,
            fork_note=self._config.fork_note,
        )
        return TurnContext(
            prompt=prompt,
            working_dir=abs_wd,
            resume_id=resume_id,
            system_prompt=system_prompt,
            model=self._config.model,
            tool_allow=self._config.tool_allow,
            tool_deny=self._config.tool_deny,
            mcp_servers=mcp_servers,
            credential=credential,
            memory_dir=self._config.memory_dir,
        )

    def build_argv(
        self,
        prompt: str,
        working_dir: str,
        resume_id: str | None = None,
        credential: HarnessCredential | None = None,
    ) -> tuple[list[str], dict[str, Any]]:
        """The pre-spawn half of a turn: assemble the context and let the
        profile render the argv. Returns `(argv, kwargs)` without spawning and
        without FS side effects — `start()` calls this then spawns; tests call
        it to inspect the command/env a turn would use."""
        ctx = self._make_context(prompt, working_dir, resume_id, credential)
        return self._profile.build_turn_argv(ctx)

    async def start(
        self,
        prompt: str,
        working_dir: str,
        resume_id: str | None = None,
        credential: HarnessCredential | None = None,
    ) -> None:
        if self._process is not None:
            raise RuntimeError("HarnessRun already started")

        ctx = self._make_context(prompt, working_dir, resume_id, credential)
        argv, kwargs = self._profile.build_turn_argv(ctx)
        argv, kwargs = prepare_spawn(argv, kwargs)

        logger.info("Spawning harness %s: %s (cwd=%s)", self._profile.backend, argv, kwargs.get("cwd"))
        self._process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=_STDOUT_LINE_LIMIT_BYTES,
            **kwargs,
        )
        self._stdout_task = asyncio.create_task(
            self._read_stdout(), name=f"{self._profile.backend}-stdout"
        )
        self._stderr_task = asyncio.create_task(
            self._read_stderr(), name=f"{self._profile.backend}-stderr"
        )

        # Codex reads stdin even with a positional prompt and blocks forever
        # waiting on EOF; closing stdin lets it proceed. Claude keeps stdin
        # open until stop() (it's how we end the turn).
        if self._profile.close_stdin_after_start and self._process.stdin is not None:
            try:
                self._process.stdin.close()
            except Exception:
                logger.debug("closing stdin failed", exc_info=True)

    async def stream(self) -> AsyncIterator[HarnessEvent]:
        while True:
            item = await self._event_queue.get()
            if item is _STREAM_END:
                return
            assert isinstance(item, HarnessEvent)
            yield item

    async def stop(self) -> None:
        """Terminate the subprocess, drain reader tasks. Idempotent."""
        proc = self._process
        if proc is None:
            return

        # Closing stdin lets the CLI exit gracefully (flush its result first).
        if proc.stdin and not proc.stdin.is_closing():
            try:
                proc.stdin.close()
            except Exception:
                pass

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

        for task in (self._stdout_task, self._stderr_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

        if not self._stream_closed:
            self._stream_closed = True
            try:
                self._event_queue.put_nowait(_STREAM_END)
            except asyncio.QueueFull:
                pass

        self._process = None
        self._stdout_task = None
        self._stderr_task = None

    async def interrupt(self) -> None:
        """Best-effort cancel of the in-flight turn. stop() does
        stdin-close → SIGTERM → SIGKILL escalation, which is sufficient;
        MCP-server children die with their parent."""
        await self.stop()

    # ------------------------------------------------------------------ helpers

    def _emit(self, event: HarnessEvent) -> None:
        if self._stream_closed:
            return
        self._event_queue.put_nowait(event)

    def _close_stream(self) -> None:
        """Signal end-of-stream to `stream()` consumers at a logical boundary
        (the terminal `result` event) before the subprocess actually exits."""
        if self._stream_closed:
            return
        self._stream_closed = True
        try:
            self._event_queue.put_nowait(_STREAM_END)
        except asyncio.QueueFull:
            pass

    @property
    def stderr_text(self) -> str:
        return "\n".join(self._stderr_lines)

    # ------------------------------------------------------------------ readers

    async def _handle_line(self, line: str) -> None:
        obj = parse_json_line(line)
        if obj is None:
            return
        out = self._parser.parse(obj)
        for event in out.events:
            self._emit(event)
        if out.end_of_stream:
            self._close_stream()

    async def _read_stdout(self) -> None:
        assert self._process and self._process.stdout
        try:
            async for raw in self._process.stdout:
                line = raw.decode(errors="replace").rstrip("\r\n")
                if not line:
                    continue
                try:
                    await self._handle_line(line)
                except Exception:
                    logger.exception(
                        "%s event parse crashed on: %s", self._profile.backend, line[:200]
                    )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("stdout reader crashed")
        finally:
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
