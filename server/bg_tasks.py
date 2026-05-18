"""Cross-turn background tasks.

The model calls `mcp__bg__run(command, description?)` to fire-and-forget
a shell command. The MCP tool POSTs into this manager, which:

  1. Inserts a `bg_tasks` row (status=running)
  2. Spawns an asyncio.Task that runs the subprocess to completion in
     the FastAPI process — independent of any `claude --print` lifetime
  3. Returns the task_id to the model immediately, so the model's turn
     can end while the subprocess keeps running

When the subprocess completes (success / failure / cancel / timeout):

  4. The row is updated with exit_code + captured output
  5. A `bg_completed` WS event is broadcast (so the chip animates)
  6. A synthesized user_message is injected into the session via
     SessionManager so the model gets a turn to react to the result

Why a FastAPI-process worker instead of letting the SDK's Bash
run_in_background handle it: that bg lives inside the per-turn `claude`
subprocess and dies with it. We need a persistent owner.

Output capping: each stream is held at MAX_STREAM_BYTES; once exceeded,
we keep the *most recent* bytes and tag the row `truncated=True`. The
synthesized prompt explains the truncation so the model can ask the
user if it needs the head bytes.
"""

from __future__ import annotations

import asyncio
import logging
import shlex
import signal
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .database import Database

logger = logging.getLogger(__name__)


# 200 KB per stream is plenty for typical script output without
# blowing up the SQLite row size. Higher cap → bigger row → slower
# WS broadcasts and DB reads. If a user really needs giant output
# they can redirect to a file inside working_dir and `/showme` it.
MAX_STREAM_BYTES = 200 * 1024

# Hard wall-clock cap. The bg task is a *fire and forget* convenience,
# not a long-running daemon — anything beyond this is almost certainly
# stuck or doing something pathological. SIGTERM at the limit, SIGKILL
# 5 s later if it doesn't respect TERM.
DEFAULT_TIMEOUT_SECONDS = 30 * 60  # 30 min


# Callback type the manager invokes when a task reaches a terminal state.
# Wired up by main.py lifespan; lets bg_tasks stay decoupled from
# session_manager / WS layer for testability.
DeliveryCallback = Callable[["BgTaskRecord"], Awaitable[None]]
BroadcastCallback = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass
class BgTaskRecord:
    """Snapshot of a bg_tasks row, as the manager hands it to callbacks.

    Kept as a plain dataclass (not a Pydantic model) because callers
    just read fields — there's no validation surface to defend.
    """

    id: str
    session_id: str
    command: str
    description: str | None
    working_dir: str
    status: str  # 'running' | 'completed' | 'failed' | 'cancelled' | 'interrupted'
    exit_code: int | None
    stdout: str
    stderr: str
    truncated: bool
    started_at: str
    completed_at: str | None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _short_id() -> str:
    """Short hex id (12 chars). Long enough to not collide in a single
    session's task list, short enough to type by hand if needed."""
    return uuid.uuid4().hex[:12]


_TRUNC_MARKER = b"\xe2\x80\xa6[truncated; tail bytes shown]\n"


def _finalize_stream(buf: bytearray, truncated: bool) -> bytes:
    """Prepend a marker if any bytes were dropped during capture.

    Cap-enforcement happens *in the reader* (which trims head bytes as
    they arrive); this is just the cosmetic finalization that tells
    the model "what you see is the tail, not the full output." Keeps
    the on-the-wire byte count predictable — marker + remaining bytes
    is still ≤ MAX_STREAM_BYTES, because we shave the marker length
    off when the buffer is right at the cap.
    """
    if not truncated:
        return bytes(buf)
    # If adding the marker would push us back over the cap, drop the
    # leading marker-length bytes to make room. Worst case the user
    # sees a 30-byte indent instead of 30 bytes of "xxxxx".
    if len(buf) + len(_TRUNC_MARKER) > MAX_STREAM_BYTES:
        drop = len(buf) + len(_TRUNC_MARKER) - MAX_STREAM_BYTES
        if drop < len(buf):
            del buf[:drop]
    return _TRUNC_MARKER + bytes(buf)


class _RunningTask:
    """Per-task state held in memory while the subprocess is alive.

    DB has the persistent copy; this class holds the live process
    handle + the asyncio.Task we await + buffers we're filling.
    """

    def __init__(self, record: BgTaskRecord, proc: asyncio.subprocess.Process) -> None:
        self.record = record
        self.proc = proc
        self.stdout_buf = bytearray()
        self.stderr_buf = bytearray()
        self.stdout_truncated = False
        self.stderr_truncated = False
        # Set when cancel_task is called; affects terminal status mapping.
        self.cancel_requested: bool = False
        # The asyncio.Task running the orchestration coroutine; held so
        # shutdown() can cancel it.
        self.task: asyncio.Task[None] | None = None


class BgTaskManager:
    """Singleton manager for cross-turn background tasks.

    Lifecycle:
        - `bind(db, deliver_cb, broadcast_cb)` once at startup
        - `start()` to flip orphaned 'running' rows to 'interrupted'
        - `start_task(...)` per model call
        - `cancel_task(...)` from MCP tool or REST UI
        - `shutdown()` on app teardown — cancels all in-flight tasks
    """

    def __init__(self) -> None:
        self._db: Database | None = None
        self._deliver_cb: DeliveryCallback | None = None
        self._broadcast_cb: BroadcastCallback | None = None
        self._running: dict[str, _RunningTask] = {}
        self._started: bool = False

    # ------------------------------------------------------------------ wiring

    def bind(
        self,
        db: Database,
        deliver_cb: DeliveryCallback,
        broadcast_cb: BroadcastCallback,
    ) -> None:
        """Wire up dependencies. Called once from main.py lifespan."""
        self._db = db
        self._deliver_cb = deliver_cb
        self._broadcast_cb = broadcast_cb

    async def start(self) -> None:
        """Mark orphaned DB rows as interrupted. Called once after bind()."""
        assert self._db is not None, "bind(db, ...) before start()"
        if self._started:
            return
        affected = await self._db.mark_in_flight_bg_tasks_interrupted(_now_iso())
        if affected:
            logger.info(
                "Marked %d bg_tasks as interrupted (left over from prior process)",
                affected,
            )
        self._started = True

    async def shutdown(self) -> None:
        """Graceful teardown: SIGTERM all running subprocesses and await
        their orchestration tasks. The DB-update path in `_run_task`
        will fire `status=cancelled` for each one so chat history is
        consistent."""
        if not self._running:
            return
        logger.info("Cancelling %d in-flight bg tasks for shutdown", len(self._running))
        for rt in list(self._running.values()):
            rt.cancel_requested = True
            await self._terminate_proc(rt)
        # Wait for orchestration tasks to wrap up DB writes.
        for rt in list(self._running.values()):
            if rt.task and not rt.task.done():
                try:
                    await asyncio.wait_for(rt.task, timeout=5.0)
                except (asyncio.TimeoutError, Exception):
                    pass

    # ------------------------------------------------------------------ public API (called by MCP / REST)

    async def start_task(
        self,
        *,
        session_id: str,
        command: str,
        working_dir: str,
        description: str | None = None,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> BgTaskRecord:
        """Create the row, spawn the process, return immediately.

        Returns the BgTaskRecord *as it was at creation*. The
        orchestration task will update the row in the DB later; callers
        that need the latest state should re-fetch via get_task().
        """
        assert self._db is not None, "BgTaskManager not bound"
        task_id = _short_id()
        started_at = _now_iso()

        # Spawn through /bin/sh -c so the model can use shell syntax
        # (pipes, redirects, &&) — same trust model as the SDK's Bash
        # tool. working_dir confines blast radius the same way.
        try:
            proc = await asyncio.create_subprocess_exec(
                "/bin/sh",
                "-c",
                command,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=working_dir,
                start_new_session=True,  # isolate process group so we can SIGTERM cleanly
            )
        except Exception as e:
            # Don't even persist a row — the task never existed. The
            # MCP tool will surface this as an error string the model
            # can read and adjust its plan.
            raise BgTaskError(f"failed to spawn bg task: {e}") from e

        record = BgTaskRecord(
            id=task_id,
            session_id=session_id,
            command=command,
            description=description,
            working_dir=working_dir,
            status="running",
            exit_code=None,
            stdout="",
            stderr="",
            truncated=False,
            started_at=started_at,
            completed_at=None,
        )
        await self._db.create_bg_task(
            task_id=task_id,
            session_id=session_id,
            command=command,
            description=description,
            working_dir=working_dir,
            started_at=started_at,
        )
        if self._broadcast_cb:
            await self._broadcast_cb(
                {
                    "type": "bg_started",
                    "session_id": session_id,
                    "task_id": task_id,
                    "command": command,
                    "description": description,
                    "started_at": started_at,
                }
            )

        rt = _RunningTask(record, proc)
        self._running[task_id] = rt
        rt.task = asyncio.create_task(
            self._run_task(rt, timeout_seconds),
            name=f"bg-task-{task_id}",
        )
        return record

    async def cancel_task(self, task_id: str) -> bool:
        """Best-effort cancel. Returns True if a live task was signalled,
        False if the task wasn't running or didn't exist in memory."""
        rt = self._running.get(task_id)
        if rt is None:
            return False
        rt.cancel_requested = True
        await self._terminate_proc(rt)
        return True

    async def list_tasks(self, session_id: str) -> list[BgTaskRecord]:
        assert self._db is not None
        rows = await self._db.list_bg_tasks_for_session(session_id)
        return [
            BgTaskRecord(
                id=r["id"],
                session_id=r["session_id"],
                command=r["command"],
                description=r["description"],
                working_dir=r["working_dir"],
                status=r["status"],
                exit_code=r["exit_code"],
                stdout=r["stdout"],
                stderr=r["stderr"],
                truncated=r["truncated"],
                started_at=r["started_at"],
                completed_at=r["completed_at"],
            )
            for r in rows
        ]

    async def get_task(self, task_id: str) -> BgTaskRecord | None:
        assert self._db is not None
        r = await self._db.get_bg_task(task_id)
        if r is None:
            return None
        return BgTaskRecord(
            id=r["id"],
            session_id=r["session_id"],
            command=r["command"],
            description=r["description"],
            working_dir=r["working_dir"],
            status=r["status"],
            exit_code=r["exit_code"],
            stdout=r["stdout"],
            stderr=r["stderr"],
            truncated=r["truncated"],
            started_at=r["started_at"],
            completed_at=r["completed_at"],
        )

    # ------------------------------------------------------------------ orchestration

    async def _run_task(self, rt: _RunningTask, timeout_seconds: int) -> None:
        """Read stdout/stderr concurrently, enforce timeout, persist on exit."""
        assert self._db is not None
        proc = rt.proc

        # Track truncation on rt directly — the reader fires concurrently,
        # may be cancelled mid-flight, and we'd lose a local return value
        # in those paths. Setting on rt is the load-bearing channel.
        async def reader(stream, buf: bytearray, which: str) -> None:
            while True:
                # Read in chunks so we can keep buf bounded without
                # waiting for EOF. 8 KB is the typical pipe block size.
                chunk = await stream.read(8192)
                if not chunk:
                    return
                buf.extend(chunk)
                if len(buf) > MAX_STREAM_BYTES:
                    # Drop the oldest bytes; keep the tail.
                    drop = len(buf) - MAX_STREAM_BYTES
                    del buf[:drop]
                    if which == "stdout":
                        rt.stdout_truncated = True
                    else:
                        rt.stderr_truncated = True

        readers = [
            asyncio.create_task(reader(proc.stdout, rt.stdout_buf, "stdout")),
            asyncio.create_task(reader(proc.stderr, rt.stderr_buf, "stderr")),
        ]

        timed_out = False
        try:
            try:
                await asyncio.wait_for(proc.wait(), timeout=timeout_seconds)
            except asyncio.TimeoutError:
                timed_out = True
                logger.warning("bg task %s timed out after %ds", rt.record.id, timeout_seconds)
                rt.cancel_requested = False  # distinct from user-initiated cancel
                await self._terminate_proc(rt)
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    pass

            # Drain readers — wait_for returns when proc exits, but
            # readers may still be flushing the last chunk.
            for r in readers:
                try:
                    await asyncio.wait_for(r, timeout=2.0)
                except (asyncio.TimeoutError, Exception):
                    r.cancel()

        except asyncio.CancelledError:
            # Shutdown path. Make sure proc is gone.
            rt.cancel_requested = True
            await self._terminate_proc(rt)
            try:
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                pass
            raise

        finally:
            # The reader set {stdout,stderr}_truncated when it had to
            # drop head bytes — finalize stamps a visible marker on
            # those streams so the model knows it's seeing the tail.
            stdout_bytes = _finalize_stream(rt.stdout_buf, rt.stdout_truncated)
            stderr_bytes = _finalize_stream(rt.stderr_buf, rt.stderr_truncated)

            exit_code = proc.returncode
            if rt.cancel_requested:
                status = "cancelled"
            elif timed_out:
                status = "failed"  # surface timeout as failure; output explains
            elif exit_code == 0:
                status = "completed"
            else:
                status = "failed"

            stdout_str = stdout_bytes.decode("utf-8", errors="replace")
            stderr_str = stderr_bytes.decode("utf-8", errors="replace")
            completed_at = _now_iso()
            truncated = rt.stdout_truncated or rt.stderr_truncated

            await self._db.update_bg_task(
                rt.record.id,
                status=status,
                exit_code=exit_code,
                stdout=stdout_str,
                stderr=stderr_str,
                truncated=truncated,
                completed_at=completed_at,
            )

            rt.record.status = status
            rt.record.exit_code = exit_code
            rt.record.stdout = stdout_str
            rt.record.stderr = stderr_str
            rt.record.truncated = truncated
            rt.record.completed_at = completed_at

            self._running.pop(rt.record.id, None)

            if self._broadcast_cb:
                try:
                    await self._broadcast_cb(
                        {
                            "type": "bg_completed",
                            "session_id": rt.record.session_id,
                            "task_id": rt.record.id,
                            "status": status,
                            "exit_code": exit_code,
                            "truncated": truncated,
                            "completed_at": completed_at,
                        }
                    )
                except Exception:
                    logger.exception("bg_completed broadcast failed")

            if self._deliver_cb:
                try:
                    await self._deliver_cb(rt.record)
                except Exception:
                    logger.exception("bg delivery callback failed for %s", rt.record.id)

    async def _terminate_proc(self, rt: _RunningTask) -> None:
        """SIGTERM the whole process group; 5s grace; SIGKILL if needed.

        We spawned with start_new_session=True, so killing the group
        sweeps up any children the model's shell command forked off
        (e.g. `bash -c "long_running &"`).
        """
        proc = rt.proc
        if proc.returncode is not None:
            return
        try:
            import os
            pgid = os.getpgid(proc.pid)
            os.killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            return
        # Don't await here — let the wait_for in _run_task notice the exit.
        # Schedule a SIGKILL fallback if the process resists SIGTERM.
        async def _kill_later() -> None:
            await asyncio.sleep(5.0)
            if proc.returncode is None:
                try:
                    import os as _os
                    _os.killpg(_os.getpgid(proc.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
        asyncio.create_task(_kill_later(), name=f"bg-kill-{rt.record.id}")


class BgTaskError(Exception):
    """Raised by manager methods on user-fixable problems (bad working_dir,
    spawn failure). The MCP tool turns this into a text error the model
    can read and adjust."""


def render_delivery_prompt(rec: BgTaskRecord) -> str:
    """Synthesize the user-message text we inject into the session when
    a bg task completes. Format is chosen to be unambiguous to the
    model: explicit status line, clear sections, marked truncation.

    The model is taught (via the system prompt addendum in
    claude_code.py) that messages prefixed with "[bg-task-result]"
    are auto-injected, not user-typed."""
    lines: list[str] = []
    desc = f" ({rec.description})" if rec.description else ""
    lines.append(
        f"[bg-task-result] Background task `{rec.id}`{desc} finished with "
        f"status `{rec.status}` (exit code {rec.exit_code})."
    )
    # Show the literal command so the model has full context even if
    # the original tool_use is far up the chat history.
    lines.append("")
    lines.append("Command:")
    lines.append(f"  {shlex.quote(rec.command) if ' ' in rec.command else rec.command}")
    if rec.truncated:
        lines.append("")
        lines.append("Note: output was truncated (head bytes dropped, tail preserved).")
    if rec.stdout:
        lines.append("")
        lines.append("stdout:")
        lines.append("```")
        lines.append(rec.stdout.rstrip())
        lines.append("```")
    if rec.stderr:
        lines.append("")
        lines.append("stderr:")
        lines.append("```")
        lines.append(rec.stderr.rstrip())
        lines.append("```")
    if not rec.stdout and not rec.stderr:
        lines.append("")
        lines.append("(no output)")
    lines.append("")
    lines.append(
        "Respond to the user with what you learned from this result. "
        "Don't re-quote the entire output — summarize. If this was an "
        "intermediate step toward a larger goal, continue with the next "
        "step."
    )
    return "\n".join(lines)


# Singleton
bg_task_manager = BgTaskManager()
