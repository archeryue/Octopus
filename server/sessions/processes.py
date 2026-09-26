"""The pool of held CLI processes: reuse, the idle reaper, the LRU cap, shutdown.

One slice of `SessionManager`, which was a single 4,017-line class with 84
methods (docs/plans/polish-2026-09.md §3 A1). Split by responsibility; the
methods are unchanged and still reached through one `session_manager` object,
so no call site moved.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from ..aio import drain_cancelled
from ..harness import HarnessRun, SubagentUpdate
from .base import (
    _HELD_PROCESS_IDLE_SECONDS,
    _HELD_STOP_TIMEOUT,
    _MAX_HELD_PROCESSES,
    _REAPER_INTERVAL_SECONDS,
    Session,
    SessionManagerBase,
    logger,
)


class ProcessesMixin(SessionManagerBase):

    async def _safe_backend_interrupt(self, backend: HarnessRun) -> None:
        """Best-effort background teardown of a wedged backend subprocess.

        Used from interrupt() so the WS caller isn't held by SIGTERM/SIGKILL
        escalation. Any failure is logged — the lock has already been
        released by then via the cancelled inner task.
        """
        try:
            await backend.interrupt()
        except Exception:
            logger.exception("Background backend.interrupt() failed")

    async def _enforce_held_cap(self, *, keep_session_id: str | None = None) -> int:
        """Drop least-recently-used held processes until at most
        `_MAX_HELD_PROCESSES` remain, never touching `keep_session_id` (the
        session that just finished a turn) or any session mid-turn.

        This is the hard bound on the memory reuse costs. The reaper's idle
        timeout is the soft one — it releases processes nobody is using, while
        this stops a busy workspace from holding more than we budgeted for.
        """
        held = [
            s
            for s in self.sessions.values()
            if s._backend is not None
            and s._held_run_at is not None
            and s.id != keep_session_id
            and (s._active_task is None or s._active_task.done())
        ]
        if len(held) < _MAX_HELD_PROCESSES:
            return 0
        held.sort(key=lambda s: s._held_run_at or 0.0)
        # `keep_session_id` occupies one slot of the budget.
        excess = len(held) - (_MAX_HELD_PROCESSES - (1 if keep_session_id else 0))
        stopped = 0
        for session in held[: max(0, excess)]:
            backend = session._backend
            self._forget_backend(session)
            if backend is None:
                continue
            try:
                await backend.stop()
                stopped += 1
            except Exception:
                logger.exception(
                    "failed stopping held process for session %s", session.id
                )
        if stopped:
            logger.info("released %d held CLI process(es) to stay under the cap", stopped)
        return stopped

    async def reap_held_processes(self) -> int:
        """Drop held CLI processes that have outstayed their welcome.

        Two rules, both from §7: anything idle longer than
        `_HELD_PROCESS_IDLE_SECONDS`, and — if more are still held than
        `_MAX_HELD_PROCESSES` — the least recently used until the count fits.
        Returns how many were stopped. A session whose process is reaped is
        not harmed: its next turn spawns and resumes.
        """
        now = time.monotonic()
        held = [
            s
            for s in self.sessions.values()
            if s._backend is not None
            and s._held_run_at is not None
            and (s._active_task is None or s._active_task.done())
        ]
        doomed = [s for s in held if now - (s._held_run_at or now) >= _HELD_PROCESS_IDLE_SECONDS]
        survivors = [s for s in held if s not in doomed]
        if len(survivors) > _MAX_HELD_PROCESSES:
            survivors.sort(key=lambda s: s._held_run_at or 0.0)
            doomed.extend(survivors[: len(survivors) - _MAX_HELD_PROCESSES])

        stopped = 0
        for session in doomed:
            backend = session._backend
            self._forget_backend(session)
            if backend is None:
                continue
            try:
                await backend.stop()
                stopped += 1
            except Exception:
                logger.exception(
                    "failed stopping held process for session %s", session.id
                )
        if stopped:
            logger.info("reaped %d idle CLI process(es)", stopped)
        return stopped

    async def _reaper_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(_REAPER_INTERVAL_SECONDS)
                await self.reap_held_processes()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("held-process reaper iteration failed")

    def start_reaper(self) -> None:
        """Start the idle-process reaper. Idempotent; called from lifespan."""
        if self._reaper_task is None or self._reaper_task.done():
            self._reaper_task = asyncio.create_task(
                self._reaper_loop(), name="held-process-reaper"
            )

    async def stop_all_held_processes(self) -> int:
        """Stop every held CLI process. Called at shutdown.

        Without this a server restart orphans one ~255MB node process per held
        session: nothing else closes their stdin, and they are waiting on it
        rather than on a parent that just died.
        """
        stopped = 0
        for session in list(self.sessions.values()):
            backend = session._backend
            self._forget_backend(session)
            if backend is None:
                continue
            try:
                # Bounded: a CLI that won't die must not hold up shutdown.
                # The process group gets SIGKILLed by stop()'s own escalation,
                # and anything still alive after that is the OS's problem, not
                # a reason to hang the server.
                await asyncio.wait_for(backend.stop(), timeout=_HELD_STOP_TIMEOUT)
                stopped += 1
            except TimeoutError:
                logger.warning(
                    "held process for session %s didn't stop in %.0fs; abandoning it",
                    session.id,
                    _HELD_STOP_TIMEOUT,
                )
            except Exception:
                logger.exception(
                    "failed stopping held process for session %s at shutdown",
                    session.id,
                )
        return stopped

    async def stop_reaper(self) -> None:
        task = self._reaper_task
        self._reaper_task = None
        await drain_cancelled(task, "held-process reaper")

    def _reusable_run(
        self,
        session: Session,
        working_dir: str,
        credential: Any,
        agent: dict[str, Any] | None,
        connectors: list[tuple[Any, Any]] | None,
    ) -> HarnessRun | None:
        """The session's held process, if it can serve this turn.

        Spawning the CLI costs ~1.5s that a live process doesn't pay, and its
        prompt cache stays warm (inline-steering.md §3). Reuse is refused —
        and the caller spawns fresh — whenever anything baked in at spawn has
        changed, so a persona edit or a credential swap can never be served by
        a process still running the old one.
        """
        held = session._backend
        if held is None or not held.reusable or not held.is_alive():
            return None
        # Never hand a process that's mid-turn to a second turn. `start_message`
        # normally prevents concurrent turns, but reuse makes a live process
        # reachable from more paths than before, and two turns sharing one
        # stdin would interleave their prompts into one conversation.
        if session._steer_open:
            logger.warning(
                "session %s: refusing to reuse a process with a turn in flight",
                session.id,
            )
            return None
        want = self._make_run(session, agent, connectors).spawn_signature(
            working_dir, credential
        )
        if held.spawn_signature(working_dir, credential) != want:
            logger.info(
                "session %s config changed; respawning instead of reusing",
                session.id,
            )
            return None
        return held

    def _forget_backend(self, session: Session) -> None:
        """Detach a session from its CLI process and close out what that
        process was still doing.

        Sub-agents live inside the process: when it goes, anything still
        marked running is over, and saying so beats a card that spins for the
        rest of the session (native-subagents.md §7).
        """
        session._backend = None
        session._held_run_at = None
        for key, run in list(session._subagents.items()):
            if run.status == "running":
                session._subagents[key] = SubagentUpdate(
                    task_id=run.task_id,
                    tool_use_id=run.tool_use_id,
                    status="failed",
                    name=run.name,
                    description="interrupted — the CLI process ended",
                    prompt=run.prompt,
                    summary=run.summary,
                    tokens=run.tokens,
                    tool_uses=run.tool_uses,
                    duration_ms=run.duration_ms,
                )
