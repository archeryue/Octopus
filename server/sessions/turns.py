"""Driving one turn: queueing, dispatch, the backend run loop, steering, interrupt.

One slice of `SessionManager`, which was a single 4,017-line class with 84
methods (docs/plans/polish-2026-09.md §3 A1). Split by responsibility; the
methods are unchanged and still reached through one `session_manager` object,
so no call site moved.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from .. import fork_helpers
from .. import monitor as _monitor
from ..aio import drain_cancelled, stopped_within
from ..attachments import MAX_ATTACHMENTS_PER_MESSAGE
from ..attachments import get_path as get_attachment_path
from ..harness import (
    Harness,
    HarnessCredential,
    HarnessEvent,
    HarnessRun,
    RunConfig,
    get_harness,
)
from ..large_prompts import spill_if_large
from ..models import AttachmentMetadata, MessageContent, MessageRole, SessionStatus
from ..monitor import Event as _MonEvent
from .base import (
    _DELTA_FLUSH_SECONDS,
    _HELD_STOP_TIMEOUT,
    _MAX_PENDING_STEERS,
    PendingQuestion,
    QueuedPrompt,
    Session,
    SessionManagerBase,
    _augment_prompt_with_attachments,
    _guess_mime,
    _split_tool_list,
    logger,
)


@dataclass
class _Attempt:
    """One CLI invocation inside a logical turn.

    These eleven fields were local variables in a 473-line `_run_backend`, and
    the fact that eight of its branches read and wrote them is precisely why
    that method could not be split: an event handler needs the same mutable
    view of "what this invocation has seen so far" as the failure classifiers
    that read it afterwards. Naming that view is what makes both extractable
    (docs/plans/polish-2026-09.md §3 A1.5).
    """

    backend: HarnessRun
    # The prompt THIS invocation ran — the original on the first pass, and
    # whatever the retry decided on later ones.
    prompt: str
    # True when the process was already holding this conversation, so the
    # prompt goes in as the next message rather than a fresh invocation.
    reused: bool
    saw_result: bool = False
    saw_tool_use: bool = False
    # Whether any assistant text streamed — gates the transient retry (don't
    # re-run a turn that already produced output).
    saw_text: bool = False
    # Terminal-error signal for post-turn auth-expiry classification.
    saw_error_event: bool = False
    error_text: str = ""
    # Streaming-text coalescing (inline-steering.md §4 S1). The CLI emits one
    # delta per token; forwarding each as its own WS frame would be a frame
    # per token and a React render per token. We batch them into at most one
    # frame every _DELTA_FLUSH_SECONDS, which is still far below the threshold
    # where a human sees stepping, and flush before any non-delta event so
    # ordering with tool calls and the final block is exact.
    delta_buf: list[str] = field(default_factory=list)
    delta_last_flush: float = 0.0
    # Per-turn watchdog state (turn-safety.md §3): the watchdog stops a turn
    # that goes silent (idle) or runs too long (overall) so it can never hang
    # forever the way the deep-research wedge did.
    watchdog: dict[str, Any] = field(
        default_factory=lambda: {"last": time.monotonic(), "tripped": None}
    )

    def touch(self) -> None:
        """Mark progress, so the idle watchdog doesn't trip on a live turn."""
        self.watchdog["last"] = time.monotonic()

    @property
    def timeout(self) -> tuple[str, int] | None:
        """`(reason, limit)` when the watchdog stopped this attempt."""
        return self.watchdog["tripped"]

    @property
    def failed(self) -> bool:
        """A turn that errored, or ended without ever saying it was done."""
        return self.saw_error_event or not self.saw_result

    @property
    def produced_output(self) -> bool:
        return self.saw_tool_use or self.saw_text


@dataclass
class _Turn:
    """What survives across the CLI invocations of one logical turn.

    The retry budgets are per turn, not per attempt — that is the whole point
    of them — and so is the resume id the turn started from.
    """

    # The prompt the user (or the injector) actually sent. A transient retry
    # with no output yet re-runs THIS, not whatever a later attempt was given.
    prompt: str
    # The prompt the next attempt will run: `prompt`, "continue", or a
    # history-replay wrapper.
    current_prompt: str
    harness: Harness
    # The effective credential id (session override, else the agent's). Used
    # to flag the right row needs_reconnect on a mid-turn 401
    # (harness-credential-reauth.md §4). None = host-default CLI auth.
    cred_id: str | None
    # The resume id this logical turn STARTED from. A transient retry re-runs
    # the original invocation, so it must restore this — a failed no-output
    # attempt can still emit `session_started` and mutate
    # session.claude_session_id, which would otherwise turn the retry into a
    # `--resume <failed-id>` of the same prompt (Vera review,
    # harness-transient-retry.md §4).
    resume_at_turn_start: str | None
    recovery_attempts: int = 0
    transient_attempts: int = 0
    # A dangling resume id is recoverable exactly once per turn: clear it,
    # start fresh. A second failure is a different problem.
    stale_session_retries: int = 0


@dataclass
class _Decision:
    """What one finished CLI invocation means: stop, or run it again.

    `events` are the frames the user sees before either happens — the
    explanation for whichever it is. Returning this rather than yielding in
    place is what lets each classifier be an ordinary method instead of a
    branch inside an async generator.
    """

    retry: bool
    events: list[dict[str, Any]] = field(default_factory=list)
    # The prompt the retry runs; None keeps whatever the turn already had.
    prompt: str | None = None
    delay: float = 0.0

    @classmethod
    def stop(cls, *events: dict[str, Any]) -> _Decision:
        return cls(retry=False, events=list(events))

    @classmethod
    def again(
        cls, prompt: str, *events: dict[str, Any], delay: float = 0.0
    ) -> _Decision:
        return cls(retry=True, events=list(events), prompt=prompt, delay=delay)


class TurnsMixin(SessionManagerBase):

    async def _persist_message(
        self,
        session: Session,
        msg: MessageContent,
        *,
        git_head: str | None = None,
        git_status_clean: bool | None = None,
    ) -> int | None:
        """Persist and return the assigned seq (or None if no DB).

        Callers tag broadcast/yield events with this seq so clients can
        dedupe against the snapshot returned by GET /api/sessions/{id}
        after a reconnect.

        `git_head` / `git_status_clean` are the turn-start anchor captured for
        user-message rows (session-rewind.md §5.6.3); None elsewhere.
        """
        if not self.db:
            return None
        seq = session._message_count
        session._message_count += 1
        await self.db.append_message(
            session_id=session.id,
            seq=seq,
            role=msg.role.value,
            type=msg.type,
            content=msg.content,
            tool_name=msg.tool_name,
            tool_input=msg.tool_input,
            tool_use_id=msg.tool_use_id,
            is_error=msg.is_error,
            session_id_ref=msg.session_id,
            cost=msg.cost,
            attachments=[a.model_dump() for a in msg.attachments] if msg.attachments else None,
            git_head=git_head,
            git_status_clean=git_status_clean,
        )
        return seq

    async def start_message(
        self,
        session_id: str,
        prompt: str,
        attachment_ids: list[str] | None = None,
        *,
        steerable: bool = False,
    ) -> None:
        """Kick off a message, or queue it if the session is already running.

        `attachment_ids` are previously-uploaded files (see
        `POST /api/sessions/{id}/attachments`). They're carried with the
        prompt through the queue and resolved to absolute paths at spawn
        time so the agent's `Read` tool can open them.

        `steerable` says this message may be handed to a turn already running
        instead of queueing behind it (inline-steering.md §8). It defaults to
        **False** and only the WebSocket composer passes True, because this is
        the injection path for bg-task results, delegation replies, schedules,
        application builds and research reports — each of which is a turn in
        its own right, with a marker prefix the model is expected to read as a
        fresh instruction. Steering one of those into the middle of an
        unrelated turn delivers it somewhere it was never meant to land.
        """
        session = self.sessions.get(session_id)
        if session is None:
            raise ValueError(f"Session {session_id} not found")

        if attachment_ids and len(attachment_ids) > MAX_ATTACHMENTS_PER_MESSAGE:
            raise ValueError(
                f"too many attachments: max {MAX_ATTACHMENTS_PER_MESSAGE}"
            )

        queued = QueuedPrompt(prompt=prompt, attachment_ids=list(attachment_ids or []))

        # Busy path. First chance: steer — hand the message to the turn that's
        # already running instead of queueing behind it (inline-steering.md §8).
        # A fork can never be in flight here, because fork_session only sets
        # `_forking` against a quiescent parent (no `_active_task`), so a
        # running turn implies not-forking — no `_forking` check needed.
        if session._active_task and not session._active_task.done():
            if steerable and await self._try_steer(session, queued):
                return
            session._pending_queue.append(queued)
            await self._broadcast(
                {
                    "type": "queued",
                    "session_id": session_id,
                    "content": prompt,
                    "queue_length": len(session._pending_queue),
                }
            )
            return

        # Idle path: the session lock is free (send_message only holds it during
        # a turn), so acquiring it here is non-blocking and gives a real mutex
        # against a racing fork_session (session-rewind.md §5.4). Under the
        # lock: refuse if a fork is mid-saga, re-check for a turn another
        # coroutine may have started while we waited, then claim `_active_task`.
        async with session._lock:
            if session._forking:
                raise ValueError(f"Session {session_id} is busy (forking)")
            if session._active_task and not session._active_task.done():
                session._pending_queue.append(queued)
                await self._broadcast(
                    {
                        "type": "queued",
                        "session_id": session_id,
                        "content": prompt,
                        "queue_length": len(session._pending_queue),
                    }
                )
                return
            session._active_task = asyncio.create_task(
                self._drive_messages(session_id, queued)
            )

    async def _drive_messages(
        self, session_id: str, initial: QueuedPrompt
    ) -> None:
        """Run the initial prompt, then drain any queued prompts.

        Each prompt runs as an inner task that interrupt() can cancel
        independently, so cancelling one prompt doesn't stop the queue.
        """
        session = self.sessions.get(session_id)
        if session is None:
            return

        current: QueuedPrompt | None = initial
        while current is not None:
            inner = asyncio.create_task(self._consume_message(session_id, current))
            session._inner_task = inner
            try:
                await inner
            except asyncio.CancelledError:
                pass  # interrupt() cancelled the inner task; continue draining
            except Exception:
                logger.exception(
                    "Background task error for session %s", session_id
                )
            finally:
                session._inner_task = None

            if session._pending_queue:
                current = session._pending_queue.pop(0)
                await self._broadcast(
                    {
                        "type": "dequeued",
                        "session_id": session_id,
                        "queue_length": len(session._pending_queue),
                    }
                )
            else:
                current = None

        # Queue is drained — fire the session-idle notifier (future-
        # features #5). Detached because notifier sends do network I/O.
        await self._fire_session_idle_notification(session)

        # Schedule-origin and delegation-origin sessions hide themselves
        # once idle so heavy fan-out doesn't pile up the active list
        # (agent-refactor.md §5.6 + agent-collaboration.md §5.2). The
        # archived rows are still browsable via the account-menu manage
        # page or the sidebar's "show delegations" toggle.
        if session.origin in self._AUTO_ARCHIVE_ORIGINS:
            await self.auto_archive_scheduled_session(session_id)

    async def deliver_bg_result(self, rec) -> bool:  # type: ignore[no-untyped-def]
        """Inject a synthesized user message into a session when a bg
        task completes. Threaded through the same start_message path
        as a real user prompt, so it queues behind an in-flight turn
        instead of racing it.

        `rec` is a server.bg_tasks.BgTaskRecord — passed by name
        rather than imported at module top to avoid a circular import
        (bg_tasks depends on Database; the manager wires the delivery
        callback into us in main.py's lifespan).

        Returns True if the session exists and the prompt was accepted,
        False if the session was already gone (e.g. deleted while the
        bg task was running). Marker `[bg-task-result]` in the prompt
        body is what the frontend keys off of for the "auto" badge —
        keeping it textual means the model also sees the marker in
        chat history on resume, which is the right cue.
        """
        from ..bg_tasks import render_delivery_prompt

        session = self.sessions.get(rec.session_id)
        if session is None:
            logger.info(
                "bg task %s completed for missing session %s; dropping result",
                rec.id,
                rec.session_id,
            )
            return False
        prompt = render_delivery_prompt(rec)
        try:
            await self.start_message(rec.session_id, prompt, attachment_ids=None)
        except Exception:
            logger.exception(
                "Failed to inject bg result for task %s into session %s",
                rec.id,
                rec.session_id,
            )
            return False
        return True

    async def _consume_message(
        self, session_id: str, queued: QueuedPrompt
    ) -> None:
        async for _event in self.send_message(
            session_id, queued.prompt, queued.attachment_ids
        ):
            pass  # send_message persists + broadcasts each event

    async def send_message(
        self,
        session_id: str,
        prompt: str,
        attachment_ids: list[str] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        session = self.sessions.get(session_id)
        if session is None:
            raise ValueError(f"Session {session_id} not found")

        try:
            await asyncio.wait_for(session._lock.acquire(), timeout=5.0)
        except TimeoutError:
            raise ValueError(f"Session {session_id} is busy")

        try:
            # Resolve attachment ids → on-disk paths so the prompt can
            # cite absolute paths the agent's `Read` tool will open.
            # Missing files are dropped (with a logged warning) rather
            # than failing the whole turn — the user already typed the
            # prompt; an orphaned id from a deleted file shouldn't eat it.
            attachments_meta: list[AttachmentMetadata] = []
            attachment_paths: list[str] = []
            for aid in attachment_ids or []:
                path = get_attachment_path(session_id, aid)
                if path is None or not path.is_file():
                    logger.warning(
                        "Session %s: dropped missing attachment %s", session_id, aid
                    )
                    continue
                # Reconstruct the user-visible filename from the on-disk
                # `<id>__<filename>` layout.
                fname = path.name.split("__", 1)[1] if "__" in path.name else path.name
                attachments_meta.append(
                    AttachmentMetadata(
                        id=aid,
                        filename=fname,
                        size=path.stat().st_size,
                        mime_type=_guess_mime(fname),
                    )
                )
                attachment_paths.append(str(path))

            # Capture the turn-start git anchor onto the user-message row
            # (session-rewind.md §5.6.3): this row's git state IS the
            # branch-point state if the user later forks here. (None, None)
            # when working_dir isn't a git repo.
            git_head, git_status_clean = await fork_helpers.capture_git_anchor(
                session.working_dir
            )

            # Record user message — content is the *raw* prompt the user
            # typed; the augmented `<attachments>` block is only what we
            # hand to the backend.
            user_msg = MessageContent(
                role=MessageRole.user,
                type="text",
                content=prompt,
                attachments=attachments_meta,
            )
            seq = await self._persist_message(
                session,
                user_msg,
                git_head=git_head,
                git_status_clean=git_status_clean,
            )
            event: dict[str, Any] = {
                "type": "user_message",
                "session_id": session_id,
                "content": prompt,
            }
            if attachments_meta:
                event["attachments"] = [a.model_dump() for a in attachments_meta]
            if seq is not None:
                event["seq"] = seq
            await self._broadcast(event)
            yield event

            session.status = SessionStatus.running
            await self._broadcast(
                {"type": "status", "session_id": session_id, "status": "running"}
            )

            # Attachments wrap the raw prompt; the `/showme` viewer flow now
            # resolves on the client + dedicated resolution endpoint instead of
            # being rewritten into a backend command.
            augmented_prompt = _augment_prompt_with_attachments(
                prompt, attachment_paths
            )

            # Fork first-turn replay (HISTORY_REPLAY backends, e.g. Codex —
            # session-rewind.md §3.5/§5.3.2). DISPATCH-ONLY: the raw user
            # text was persisted/broadcast above; only the prompt the backend
            # subprocess sees is wrapped with the truncated parent transcript
            # (the fork's own copied prefix, seq <= fork_after_seq). The flag
            # clears after the first `result` lands, so turn 2+ isn't wrapped.
            if session.fork_needs_replay and self.db:
                cutoff = (
                    session.fork_after_seq
                    if session.fork_after_seq is not None
                    else -1
                )
                # Bounded in SQL rather than loaded whole and filtered: the
                # prefix is all that is wanted, and on a long session the
                # discarded remainder is the bulk of it (B3).
                copied = [
                    MessageContent(**m)
                    for m in await self.db.load_messages(session.id, max_seq=cutoff)
                ]
                augmented_prompt = fork_helpers.wrap_for_fork_replay(
                    augmented_prompt, copied
                )

            # Spill prompts that would blow Linux's MAX_ARG_STRLEN
            # (~128 KB per argv element) to a per-session file and
            # hand the backend a small pointer instead. Triggers most
            # often on bg-task-result injection of large test-suite
            # output. See server/large_prompts.py.
            backend_dispatch_prompt = spill_if_large(session_id, augmented_prompt)

            try:
                async for ws_event in self._run_backend(session, backend_dispatch_prompt):
                    await self._broadcast(ws_event)
                    yield ws_event
            except Exception as e:
                logger.exception("Backend error in session %s", session_id)
                error_msg = MessageContent(
                    role=MessageRole.system,
                    type="error",
                    content=str(e),
                )
                err_seq = await self._persist_message(session, error_msg)
                event = {
                    "type": "error",
                    "session_id": session_id,
                    "message": str(e),
                }
                if err_seq is not None:
                    event["seq"] = err_seq
                await self._broadcast(event)
                yield event
            finally:
                if self.db:
                    await self.db.flush()
                session.status = SessionStatus.idle
                await self._broadcast(
                    {"type": "status", "session_id": session_id, "status": "idle"}
                )
        finally:
            session._lock.release()

    async def interrupt(self, session_id: str) -> bool:
        _monitor.record(_MonEvent(kind="turn_interrupt", session_id=session_id))
        """Cancel the currently running prompt. Queued prompts continue.

        Best-effort: if the backend subprocess is wedged (e.g. waiting on
        a control_response we'll never send), interrupt still releases the
        UI immediately by cancelling the inner task — the subprocess gets
        torn down in the background. We never block the caller on
        backend.interrupt(), which can take seconds for stdin-close →
        SIGTERM → SIGKILL escalation.
        """
        session = self.sessions.get(session_id)
        if session is None:
            return False

        # Fire backend teardown in the background and return fast. The
        # inner task cancellation below releases session._lock via
        # send_message's finally clause, so new turns become possible
        # even before the subprocess actually exits.
        if session._backend:
            backend = session._backend
            # Let go of it immediately. The teardown task below holds its own
            # reference, but the session must not: an interrupted process is
            # being torn down, and with reuse a next turn that found it still
            # briefly alive would hand its prompt to a dying CLI.
            self._forget_backend(session)
            asyncio.create_task(self._safe_backend_interrupt(backend))

        inner = session._inner_task
        had_active = inner is not None and not inner.done()
        if had_active:
            inner.cancel()
        elif session._lock.locked():
            # Wedged state: no live task to cancel but the lock is still
            # held (typically: previous turn's task got cancelled but its
            # finally clause was bypassed somehow). Force-release so the
            # UI isn't soft-locked. Distinguish this from a truly idle
            # session, which should return False below.
            try:
                session._lock.release()
            except RuntimeError:
                pass
            session.status = SessionStatus.idle
            await self._broadcast(
                {"type": "status", "session_id": session_id, "status": "idle"}
            )
        else:
            # Truly idle — nothing to interrupt.
            return False

        session._pending_questions.clear()
        self._cancel_all_question_timers(session)

        marker = MessageContent(
            role=MessageRole.system,
            type="error",
            content="(interrupted by user)",
        )
        marker_seq = await self._persist_message(session, marker)
        event: dict[str, Any] = {
            "type": "error",
            "session_id": session_id,
            "message": "(interrupted by user)",
        }
        if marker_seq is not None:
            event["seq"] = marker_seq
        await self._broadcast(event)
        return True

    async def reset_session(self, session_id: str) -> None:
        """Force-reset a stuck session."""
        session = self.sessions.get(session_id)
        if session is None:
            raise ValueError(f"Session {session_id} not found")
        session._pending_queue.clear()
        if session._inner_task and not session._inner_task.done():
            session._inner_task.cancel()
        if session._active_task and not session._active_task.done():
            session._active_task.cancel()
        if session._backend:
            await stopped_within(
                session._backend.stop(), f"session {session.id} backend"
            )
            self._forget_backend(session)
        if session._lock.locked():
            session._lock.release()
        session.status = SessionStatus.idle
        session._pending_approvals.clear()
        session._pending_questions.clear()
        self._cancel_all_question_timers(session)
        await self._broadcast(
            {"type": "status", "session_id": session_id, "status": "idle"}
        )

    # ------------------------------------------------------------------ #
    # One CLI invocation: acquire a process, start it, retire it
    # ------------------------------------------------------------------ #

    async def _acquire_backend(
        self,
        session: Session,
        credential: HarnessCredential | None,
        agent: dict[str, Any] | None,
        connectors: list[tuple[Any, Any]] | None,
    ) -> tuple[HarnessRun, bool]:
        """The process this attempt runs in, and whether it already holds the
        conversation.

        Reuse the session's live process when nothing it baked in at spawn has
        changed (inline-steering.md §7); otherwise spawn. Declining to reuse
        must not simply overwrite the handle: that orphans a live ~255MB
        process that nothing points at any more, so neither the reaper nor
        shutdown can ever reach it. Let it go properly first.
        """
        reused = self._reusable_run(
            session, session.working_dir, credential, agent, connectors
        )
        if reused is None and session._backend is not None:
            stale = session._backend
            self._forget_backend(session)
            try:
                await asyncio.wait_for(stale.stop(), timeout=_HELD_STOP_TIMEOUT)
            except (TimeoutError, Exception):
                logger.warning(
                    "session %s: abandoning a process we couldn't stop", session.id
                )
        backend = reused or self._make_run(session, agent, connectors)
        session._backend = backend
        return backend, reused is not None

    async def _start_attempt(
        self,
        session: Session,
        attempt: _Attempt,
        credential: HarnessCredential | None,
    ) -> None:
        """Hand this attempt's prompt to the process.

        A reused process already holds this conversation, so there is no prompt
        to re-render and no transcript to resume: just hand it the next
        message.
        """
        if attempt.reused:
            await attempt.backend.send_turn(attempt.prompt)
        else:
            await attempt.backend.start(
                attempt.prompt,
                session.working_dir,
                session.claude_session_id,
                credential=credential,
            )

    async def _open_steering_window(
        self, session: Session, backend: HarnessRun
    ) -> asyncio.Task[int] | None:
        """Open the steering window and start the writer that drains it.

        Open from here until `result` (inline-steering.md §8). Only a backend
        that takes input on stdin can be steered; everything else keeps
        queueing, so it gets no writer.
        """
        if not backend.reusable:
            return None
        async with session._steer_lock:
            session._steer_open = True
            if session._steer_queue:
                session._steer_ready.set()
        return asyncio.create_task(
            self._steer_writer(session, backend),
            name=f"steer-writer-{session.id}",
        )

    async def _close_steering_window(
        self, session: Session, steer_writer: asyncio.Task | None
    ) -> None:
        """Shut the steering window and hand back anything the writer missed.

        Ordering matters and is the reason this runs before anything else
        touches the process: once the window is shut, a frame written now would
        start a fresh turn rather than steer this one. A steer that missed its
        turn is not dropped — it becomes a normal queued prompt and runs next
        (inline-steering.md §9).
        """
        async with session._steer_lock:
            session._steer_open = False
        await drain_cancelled(steer_writer, f"session {session.id} steer writer")
        async with session._steer_lock:
            if session._steer_queue:
                logger.info(
                    "session %s: %d steer(s) missed the turn; queuing them",
                    session.id,
                    len(session._steer_queue),
                )
                session._pending_queue.extend(session._steer_queue)
                session._steer_queue.clear()
            session._steer_ready.clear()

    async def _stop_watchdog(self, watchdog: asyncio.Task | None) -> None:
        """Cancel the turn watchdog and wait for it to finish unwinding."""
        await drain_cancelled(watchdog, "turn watchdog")

    @staticmethod
    def _turn_ended_cleanly(attempt: _Attempt) -> bool:
        """Whether the CLI process is worth keeping for the next turn.

        Only a turn that ended the way a turn is supposed to — a clean
        `result`, nothing tripped, nothing to retry. A watchdog timeout, an
        error or an interrupt all leave the CLI in a state we would rather not
        inherit, so those shut it down and the next turn spawns fresh.
        """
        return (
            attempt.backend.reusable
            and attempt.backend.is_alive()
            and attempt.saw_result
            and not attempt.saw_error_event
            and attempt.timeout is None
        )

    async def _retire_or_hold_process(
        self, session: Session, attempt: _Attempt
    ) -> None:
        """Hold the process for the next turn, or shut it down now."""
        if self._turn_ended_cleanly(attempt):
            session._held_run_at = time.monotonic()
            # Enforce the cap HERE, not only on the reaper's tick. The reaper
            # is a background task that doesn't exist in tests and runs every
            # 30s in production, so leaving the bound to it means N finished
            # sessions can each pin ~255MB in between — which is exactly how
            # the backend suite got OOM-killed.
            await self._enforce_held_cap(keep_session_id=session.id)
            return
        try:
            await attempt.backend.stop()
        except Exception:
            logger.exception(
                "backend.stop() failed cleanly for session %s", session.id
            )
        self._forget_backend(session)

    # ------------------------------------------------------------------ #
    # One event off the stream
    # ------------------------------------------------------------------ #

    def _coalesce_delta(
        self, session: Session, attempt: _Attempt, event: HarnessEvent
    ) -> dict[str, Any] | None:
        """Buffer a token delta, emitting a frame at most every flush window."""
        if event.content:
            attempt.delta_buf.append(event.content)
        now = time.monotonic()
        if now - attempt.delta_last_flush < _DELTA_FLUSH_SECONDS:
            return None
        attempt.delta_last_flush = now
        return self._flush_text_deltas(session.id, attempt.delta_buf)

    async def _remember_resume_id(self, session: Session, resume_id: str) -> None:
        """Persist the engine-side conversation id this session resumes from."""
        if session.claude_session_id == resume_id:
            return
        session.claude_session_id = resume_id
        if self.db:
            await self.db.update_session_field(
                session.id, claude_session_id=resume_id
            )

    def _note_question_request(
        self, session: Session, question_id: str, tool_input: dict[str, Any] | None
    ) -> None:
        """Track pending question state for reconnect re-render."""
        session._pending_questions[question_id] = PendingQuestion(
            question_id=question_id,
            questions=(tool_input or {}).get("questions") or [],
        )
        self._schedule_question_timeout(session, question_id)

    async def _handle_result_event(
        self, session: Session, attempt: _Attempt, event: HarnessEvent
    ) -> None:
        """The turn said it was done: record it and settle the session."""
        attempt.saw_result = True
        _monitor.record(_MonEvent(
            kind="turn",
            session_id=session.id,
            agent_id=session.agent_id,
            backend=session.backend,
            duration_ms=event.duration_ms,
            ok=not event.is_error,
            detail={"cost": event.cost, "num_turns": event.num_turns},
        ))
        # Shut the steering window first: past this point the CLI is idle, and
        # a frame written now would start a fresh turn rather than steer this
        # one.
        async with session._steer_lock:
            session._steer_open = False
        # Update the resume id in case the CLI reissued a different one
        # mid-stream.
        if event.session_id:
            await self._remember_resume_id(session, event.session_id)
        # First fork turn produced a result: drop the ephemeral fork state so
        # turn 2+ behaves like a normal resumed session (session-rewind.md
        # §5.3.2/§5.6.5).
        await self._clear_fork_first_turn_state(session)

    @staticmethod
    def _capture_error_text(attempt: _Attempt, event: HarnessEvent) -> None:
        """Keep a failed turn's own words for the classifiers that run after it
        (harness-credential-reauth.md §4). `tool_result` errors never get here
        — a tool failing isn't the turn failing."""
        attempt.saw_error_event = True
        if event.content:
            attempt.error_text += event.content + "\n"
        if event.raw:
            try:
                attempt.error_text += json.dumps(event.raw) + "\n"
            except (TypeError, ValueError):
                pass

    async def _handle_stream_event(
        self, session: Session, attempt: _Attempt, event: HarnessEvent
    ) -> list[dict[str, Any]]:
        """One harness event → the WS frames it produces, in order.

        The dispatch the whole loop reduces to. The event vocabulary is
        normalized and enumerated in `harness/events.py`, so this branches on a
        contract rather than on whatever a CLI happened to print.
        """
        if event.type == "text_delta":
            flushed = self._coalesce_delta(session, attempt, event)
            return [flushed] if flushed is not None else []

        frames: list[dict[str, Any]] = []
        # Anything else ends the current delta run: flush it first so the
        # partial text can never arrive after the completed block that
        # supersedes it.
        flushed = self._flush_text_deltas(session.id, attempt.delta_buf)
        if flushed is not None:
            attempt.delta_last_flush = time.monotonic()
            frames.append(flushed)

        # session_started arrives on the CLI's init event, before any tool
        # work. Persist the resume id immediately so the recovery path can use
        # it even if the bug suppresses `result`. Internal event — never
        # persisted or broadcast.
        if event.type == "session_started" and event.session_id:
            await self._remember_resume_id(session, event.session_id)
            return frames

        # A sub-agent's progress: remembered on the session so a reload can
        # still paint the card, broadcast so the open UI paints it now, and
        # never persisted — the Task tool call and its result are the durable
        # record (native-subagents.md §4).
        if event.type == "subagent" and event.subagent is not None:
            self._record_subagent(session, event.subagent)

        if event.type == "tool_use":
            attempt.saw_tool_use = True
        if event.type == "text" and event.content and event.content.strip():
            attempt.saw_text = True

        # Persist whichever message shape this event maps to. The returned seq
        # goes onto the WS event so reconnecting clients can dedupe against
        # their snapshot.
        msg_content = self._event_to_message_content(event)
        msg_seq: int | None = None
        if msg_content is not None:
            msg_seq = await self._persist_message(session, msg_content)

        if event.type == "question_request" and event.tool_use_id:
            self._note_question_request(
                session, event.tool_use_id, event.tool_input
            )
        if event.type == "result":
            await self._handle_result_event(session, attempt, event)
        # A failed `result` or an `error` event is the turn failing; a
        # `tool_result` error is not.
        if event.type in ("result", "error") and event.is_error:
            self._capture_error_text(attempt, event)

        # Translate into the WS message shape the front-end expects.
        ws_event = self._event_to_ws_message(session.id, event)
        if ws_event is not None:
            if msg_seq is not None:
                ws_event["seq"] = msg_seq
            frames.append(ws_event)
        return frames

    # ------------------------------------------------------------------ #
    # What a finished attempt means: stop, or run the CLI again
    # ------------------------------------------------------------------ #

    @staticmethod
    def _failure_blob(attempt: _Attempt) -> str:
        """Everything a failed attempt said, for the pattern matchers: the
        terminal event's content/raw plus the CLI's stderr, capped.

        getattr: a real HarnessRun exposes stderr_text; lightweight test and
        backend stand-ins may not.
        """
        stderr_text = getattr(attempt.backend, "stderr_text", "") or ""
        return (attempt.error_text + "\n" + stderr_text)[:8000]

    async def _timeout_decision(
        self, session: Session, turn: _Turn, attempt: _Attempt
    ) -> _Decision | None:
        """Turn watchdog tripped (idle or overall cap): the backend was stopped
        mid-turn. Surface a clear error and STOP — this runs before the
        auth/transient/premature-exit classifiers so a timeout is never
        mis-read as transient or respawned (turn-safety.md §3)."""
        if attempt.timeout is None:
            return None
        reason, limit = attempt.timeout
        return _Decision.stop(
            await self._surface_turn_timeout(
                session, reason=reason, limit=limit, backend=turn.harness.backend
            )
        )

    async def _auth_decision(
        self, session: Session, turn: _Turn, blob: str
    ) -> _Decision | None:
        """Reactive auth-expiry: a failed turn whose error text matches this
        backend's auth-rejection patterns means the bound credential is dead
        (revoked / rotated / expired past what the proactive refresh caught).
        Flag it needs_reconnect and surface a re-authorize prompt, then STOP —
        re-auth won't fix itself, and a retry just burns the budget
        (harness-credential-reauth.md §4)."""
        if not turn.harness.is_auth_error(blob):
            return None
        return _Decision.stop(
            await self._surface_auth_expiry(
                session, cred_id=turn.cred_id, backend=turn.harness.backend
            )
        )

    async def _stale_session_decision(
        self, session: Session, turn: _Turn, blob: str
    ) -> _Decision | None:
        """Dangling resume id: the engine no longer holds the conversation this
        session is pinned to (its local transcript was rotated or cleaned; ours
        lives in the DB and is intact).

        This is NOT transient — retrying the same id fails identically forever,
        which bricks the session silently: a result with zero turns, zero cost
        and no text. Drop the dead id and re-run the same prompt once as a
        fresh engine-side conversation, saying out loud that the engine lost
        its own history so the model starts this turn without it.
        """
        if not (
            session.claude_session_id
            and turn.stale_session_retries < 1
            and turn.harness.is_stale_session_error(blob)
        ):
            return None
        turn.stale_session_retries += 1
        logger.warning(
            "Session %s: resume id %s is gone from the engine; "
            "clearing it and starting a fresh conversation",
            session.id,
            session.claude_session_id,
        )
        session.claude_session_id = None
        turn.resume_at_turn_start = None
        if self.db:
            await self.db.update_session_field(session.id, claude_session_id=None)
        # Don't restart cold: Octopus still has the whole conversation (the
        # engine's transcript is a cache of ours, not the record), so replay
        # its tail into this turn through the same channel a fork uses when its
        # backend can't resume natively. The model continues the conversation
        # instead of appearing to forget it.
        replayed = 0
        omitted = 0
        recovery_prompt = turn.prompt
        if self.db:
            history = [
                MessageContent(**m) for m in await self.db.load_messages(session.id)
            ]
            kept, omitted = fork_helpers.select_lost_history(history)
            replayed = len(kept)
            if kept:
                recovery_prompt = spill_if_large(
                    session.id,
                    fork_helpers.wrap_for_lost_history(
                        turn.prompt, kept, omitted=omitted
                    ),
                )
        return _Decision.again(
            recovery_prompt,
            await self._surface_stale_session(
                session,
                backend=turn.harness.backend,
                replayed=replayed,
                omitted=omitted,
            ),
        )

    async def _transient_decision(
        self, session: Session, turn: _Turn, attempt: _Attempt, blob: str
    ) -> _Decision | None:
        """Transient provider-reliability failure (5xx / overloaded / dropped
        connection / server-side throttle) → bounded retry.

        TWO modes, by whether the turn already produced output:

        * **no output yet** → re-run the ORIGINAL prompt from the turn-start
          resume state (side-effect-free; discard any resume id a failed
          no-output attempt captured — Vera).
        * **output already streamed** (tool_use/text) AND a resume id was
          captured → RESUME with "continue" so we pick up where it left off
          WITHOUT re-running tools or duplicating text. This is the common
          case: a long agent turn throttled mid-flight — the earlier
          no-output-only gate let it stop.

        Quota/credit errors match no pattern here → they surface as-is
        (harness-transient-retry.md §4).
        """
        if not turn.harness.is_transient_error(blob):
            return None
        can_retry = turn.transient_attempts < self._MAX_TRANSIENT_RETRIES and (
            not attempt.produced_output or bool(session.claude_session_id)
        )
        if not can_retry:
            # Budget exhausted (or output with no resume id to continue from) —
            # surface a clear error so the user knows it wasn't their request
            # that failed.
            return _Decision.stop(
                await self._surface_transient_exhausted(
                    session,
                    backend=turn.harness.backend,
                    attempts=turn.transient_attempts,
                )
            )

        turn.transient_attempts += 1
        delay = self._TRANSIENT_RETRY_BASE_DELAY * (2 ** (turn.transient_attempts - 1))
        logger.warning(
            "Session %s: transient backend error; retrying in "
            "%.1fs (attempt %d/%d, resume=%s)",
            session.id,
            delay,
            turn.transient_attempts,
            self._MAX_TRANSIENT_RETRIES,
            attempt.produced_output,
        )
        if attempt.produced_output:
            # Continue the in-progress conversation from its captured resume
            # id — no re-run, no duplication.
            next_prompt = "continue"
        else:
            next_prompt = turn.prompt  # original invocation
            if session.claude_session_id != turn.resume_at_turn_start:
                session.claude_session_id = turn.resume_at_turn_start
                if self.db:
                    await self.db.update_session_field(
                        session.id, claude_session_id=turn.resume_at_turn_start
                    )
        return _Decision.again(
            next_prompt,
            await self._surface_transient_retry(
                session,
                attempt=turn.transient_attempts,
                max_attempts=self._MAX_TRANSIENT_RETRIES,
                delay=delay,
            ),
            delay=delay,
        )

    async def _premature_exit_decision(
        self, session: Session, turn: _Turn, attempt: _Attempt
    ) -> _Decision:
        """Whether to respawn after the CLI's premature-exit bug.

        The signature is: the CLI exited without a `result` event AFTER
        emitting a `tool_use`. Anything else — a clean turn, an immediate crash
        with no tool use, a turn we've already retried once — is left alone.
        This is the last word on an attempt, so it always decides.
        """
        if attempt.saw_result:
            return _Decision.stop()
        if not turn.harness.premature_exit_recovery:
            # Harness opts out of the Claude-CLI premature-exit recovery (Codex
            # runs exactly once per turn) — codex-backend.md §5.6.
            return _Decision.stop()
        if turn.recovery_attempts >= self._MAX_RECOVERY_ATTEMPTS:
            logger.warning(
                "Session %s: CLI premature-exit retry budget exhausted; "
                "giving up on this turn", session.id
            )
            return _Decision.stop()
        if not attempt.saw_tool_use:
            return _Decision.stop()
        if not session.claude_session_id:
            # No resume id captured (init never arrived) — we can't respawn
            # into the same conversation.
            return _Decision.stop()

        turn.recovery_attempts += 1
        logger.warning(
            "Session %s: detected CLI premature-exit after tool_use; "
            "auto-respawning with 'continue' (attempt %d/%d)",
            session.id, turn.recovery_attempts, self._MAX_RECOVERY_ATTEMPTS,
        )
        # Persist a discreet system marker so the UI / transcript records that
        # a recovery happened. Uses the same shape as the (interrupted by user)
        # marker in interrupt().
        marker = MessageContent(
            role=MessageRole.system,
            type="error",
            content="(auto-resumed after CLI exited mid-turn)",
        )
        marker_seq = await self._persist_message(session, marker)
        marker_event: dict[str, Any] = {
            "type": "error",
            "session_id": session.id,
            "message": "(auto-resumed after CLI exited mid-turn)",
        }
        if marker_seq is not None:
            marker_event["seq"] = marker_seq
        return _Decision.again("continue", marker_event)

    async def _after_attempt(
        self, session: Session, turn: _Turn, attempt: _Attempt
    ) -> _Decision:
        """Classify a finished CLI invocation.

        The order is the design, not a preference. A watchdog timeout is read
        first so it can never be mistaken for something retryable. Auth comes
        before the retries because re-auth won't fix itself. A dangling resume
        id comes before transient because it looks transient and is not.
        Premature-exit recovery is last and always answers, because "the CLI
        just stopped" is only knowable once nothing else claimed the failure.
        """
        decision = await self._timeout_decision(session, turn, attempt)
        if decision is not None:
            return decision

        if attempt.failed:
            blob = self._failure_blob(attempt)
            decision = await self._auth_decision(session, turn, blob)
            if decision is not None:
                return decision
            decision = await self._stale_session_decision(session, turn, blob)
            if decision is not None:
                return decision
            decision = await self._transient_decision(session, turn, attempt, blob)
            if decision is not None:
                return decision

        return await self._premature_exit_decision(session, turn, attempt)

    async def _run_backend(
        self, session: Session, prompt: str
    ) -> AsyncIterator[dict[str, Any]]:
        """Drive one logical turn through the backend, recovering from
        CLI premature-exit-after-tool-roundtrip if it fires.

        Each iteration of the loop is one CLI invocation. It normally runs
        exactly once and returns after a `result` event; `_after_attempt` is
        what decides otherwise — a bounded retry for a transient failure, a
        fresh conversation for a dangling resume id, a `"continue"` respawn for
        the premature-exit bug post-mortemed in
        docs/post-mortems/2026-05-18-bg-pipeline-hardening.md §2.
        """
        # Load the owning agent fresh each turn — this is the live-reference
        # point: editing an agent's prompt/model/tools/MCP affects its
        # already-open sessions on their next turn (agent-refactor.md §5.2).
        agent = await self._load_agent(session)
        harness = get_harness(session.backend)
        credential = await self._resolve_credential(session, agent, harness)
        connectors = await self._load_connectors(agent)
        turn = _Turn(
            prompt=prompt,
            current_prompt=prompt,
            harness=harness,
            cred_id=session.credential_id
            or (agent.get("credential_id") if agent else None),
            resume_at_turn_start=session.claude_session_id,
        )

        while True:
            backend, reused = await self._acquire_backend(
                session, credential, agent, connectors
            )
            attempt = _Attempt(
                backend=backend, prompt=turn.current_prompt, reused=reused
            )
            watchdog = self._start_turn_watchdog(backend, attempt.watchdog)
            # Bound before the try: the `finally` reads it, and a spawn that
            # raises would otherwise unwind through an unbound name and hide
            # the real error.
            steer_writer: asyncio.Task[int] | None = None

            try:
                await self._start_attempt(session, attempt, credential)
                steer_writer = await self._open_steering_window(session, backend)
                async for event in backend.stream():
                    attempt.touch()
                    for frame in await self._handle_stream_event(
                        session, attempt, event
                    ):
                        yield frame
            finally:
                # Close the steering window and stop the writer before anything
                # else touches the process (§9), then decide the process's fate.
                await self._close_steering_window(session, steer_writer)
                await self._stop_watchdog(watchdog)
                await self._retire_or_hold_process(session, attempt)

            decision = await self._after_attempt(session, turn, attempt)
            for frame in decision.events:
                yield frame
            if not decision.retry:
                return
            if decision.prompt is not None:
                turn.current_prompt = decision.prompt
            if decision.delay:
                await asyncio.sleep(decision.delay)

    async def _load_agent(self, session: Session) -> dict[str, Any] | None:
        """Fetch the session's owning agent row (or None for legacy/no-DB)."""
        if self.db is None or not session.agent_id:
            return None
        return await self.db.get_agent(session.agent_id)

    async def _load_connectors(
        self, agent: dict[str, Any] | None
    ) -> list[tuple[Any, Any]]:
        """The agent's enabled connectors as (ConnectorBase, installation)
        tuples — loaded fresh each turn (same live-reference contract as the
        agent itself). Kinds no longer registered are skipped."""
        if self.db is None or agent is None:
            return []
        from ..connectors.base import ConnectorInstallation
        from ..connectors.custom import resolve_connector

        rows = await self.db.get_enabled_connectors_for_agent(agent["id"])
        out: list[tuple[Any, Any]] = []
        for row in rows:
            connector = await resolve_connector(self.db, row["kind"])
            if connector is not None:
                out.append((connector, ConnectorInstallation.from_row(row)))
        return out

    def _make_run(
        self,
        session: Session,
        agent: dict[str, Any] | None = None,
        connectors: list[tuple[Any, Any]] | None = None,
    ) -> HarnessRun:
        """Build the per-turn run for a session via its harness. Single seam
        the run loop calls (and tests monkeypatch); dispatches on
        `session.backend` through the registry — no kind branching here."""
        run = get_harness(session.backend).create_run(
            self._run_config(session, agent, connectors)
        )
        # A held process keeps working after a turn ends — an asynchronous
        # sub-agent is still running, and the CLI wakes the agent to report it
        # when it lands. Those events belong to the session
        # (native-subagents.md §7).
        run.set_idle_handler(
            lambda event, sid=session.id: self._handle_idle_event(sid, event)
        )
        return run

    async def _handle_idle_event(self, session_id: str, event: HarnessEvent) -> None:
        """An event that arrived on a held process with no turn in flight.

        The CLI can produce a turn's worth of work nobody asked for at that
        moment: an async sub-agent finishing (`Async agent launched
        successfully` returns immediately and the work continues), the
        follow-up turn where the agent reports the result, a native cron tick.
        Dropping those — what happened before — loses the answer outright and
        leaves the sub-agent card spinning forever.

        They are handled exactly like in-turn events, minus the turn
        machinery: persisted if they map to a message, broadcast either way.
        Session status is deliberately NOT flipped to `running`: no
        `_active_task` exists to interrupt, and claiming otherwise would offer
        the user a stop button that stops nothing.
        """
        session = self.sessions.get(session_id)
        if session is None:
            return

        # Token deltas are a live view of a block that is about to arrive
        # complete. Out of turn there is nothing live to view — the turn's
        # stream is closed and the UI has already painted its final text — so
        # delivering them repaints a stale partial *after* the answer. Drop
        # them; the completed `text` that follows is the real event.
        if event.type == "text_delta":
            return

        if event.type == "subagent" and event.subagent is not None:
            self._record_subagent(session, event.subagent)
        elif event.type == "session_started" and event.session_id:
            if session.claude_session_id != event.session_id:
                session.claude_session_id = event.session_id
                if self.db:
                    await self.db.update_session_field(
                        session_id, claude_session_id=event.session_id
                    )
            return

        msg_content = self._event_to_message_content(event)
        msg_seq: int | None = None
        if msg_content is not None:
            msg_seq = await self._persist_message(session, msg_content)

        ws_event = self._event_to_ws_message(session_id, event)
        if ws_event is not None:
            if msg_seq is not None:
                ws_event["seq"] = msg_seq
            await self._broadcast(ws_event)

    async def _try_steer(self, session: Session, queued: QueuedPrompt) -> bool:
        """Offer a message to the running turn. True if it was accepted.

        Everything that isn't accepted falls through to the normal queue, so
        the message always runs — the only question is whether it reaches the
        agent mid-turn or as the next turn. Refused when the window is shut
        (the turn is finishing), when the backlog is full, or when the message
        carries attachments, which the frame channel doesn't take.
        """
        if queued.attachment_ids:
            return False
        async with session._steer_lock:
            if not session._steer_open:
                return False
            if len(session._steer_queue) >= _MAX_PENDING_STEERS:
                logger.info(
                    "session %s: steer backlog full, queueing instead", session.id
                )
                return False
            session._steer_queue.append(queued)
            session._steer_ready.set()
        return True

    async def _steer_writer(self, session: Session, backend: HarnessRun) -> int:
        """The turn's only writer of mid-turn user frames.

        Waits on `_steer_ready` rather than on the event stream, because during
        a long tool call no events arrive and a steer would otherwise sit
        unwritten for exactly as long as the tool runs. Returns when cancelled;
        the count of frames written is tracked on the session so the run loop
        can tell whether anything was delivered after `result`.
        """
        written = 0
        while True:
            await session._steer_ready.wait()
            session._steer_ready.clear()
            while True:
                # The lock is held ACROSS the write, not just around the pop.
                # Closing the window takes the same lock, so a close can never
                # interleave with a write already under way: the frame is
                # either fully delivered while the turn was live, or never
                # written at all. Without that, a half-written frame could land
                # after the CLI went idle and be taken as a whole new turn,
                # whose reply nobody is reading.
                async with session._steer_lock:
                    if not session._steer_open or not session._steer_queue:
                        break
                    queued = session._steer_queue[0]
                    try:
                        await backend.send_user_frame(queued.prompt)
                    except Exception:
                        logger.exception(
                            "failed writing steer for session %s; it will be "
                            "queued as the next turn instead",
                            session.id,
                        )
                        # Left in the queue: the turn's cleanup moves it to the
                        # normal pending queue, so the message still runs.
                        return written
                    session._steer_queue.pop(0)
                written += 1
                # Persist + broadcast only once it's actually been handed over,
                # so the transcript order matches what the engine saw.
                await self._persist_message(
                    session,
                    MessageContent(
                        role=MessageRole.user, type="text", content=queued.prompt
                    ),
                )
                await self._broadcast(
                    {
                        "type": "steered",
                        "session_id": session.id,
                        "content": queued.prompt,
                    }
                )
        return written

    def _run_config(
        self,
        session: Session,
        agent: dict[str, Any] | None = None,
        connectors: list[tuple[Any, Any]] | None = None,
    ) -> RunConfig:
        """Build the per-turn RunConfig from the (freshly-loaded) agent. The
        agent supplies the system prompt, model, built-in MCP set, and tool
        allow/deny policy (agent-refactor.md §5.2). The harness this is handed
        to (`get_harness(session.backend).create_run(config)`) renders it the
        way its profile dictates — no backend-kind branching here."""
        system_prompt: str | None = None
        model: str | None = None
        mcp_servers: list[str] | None = None
        tool_allow: list[str] | None = None
        tool_deny: list[str] | None = None
        if agent:
            system_prompt = agent.get("system_prompt") or None
            model = agent.get("model") or None
            servers = agent.get("mcp_servers")
            mcp_servers = list(servers) if servers is not None else None
            tool_allow = _split_tool_list(agent.get("tool_allow"))
            tool_deny = _split_tool_list(agent.get("tool_deny"))

        # Per-agent native memory (docs/plans/memory.md): derive the agent's
        # canonical memory dir and ensure it exists. None when there's no agent
        # → memory wiring is inert. Both harnesses point at this one dir —
        # Claude via CLAUDE_COWORK_MEMORY_PATH_OVERRIDE, Codex via its blurb —
        # so memory never touches CLAUDE_CONFIG_DIR / CODEX_HOME (auth + resume
        # stay put).
        memory_dir: str | None = None
        if session.agent_id:
            from .. import agent_memory

            agent_memory.ensure_agent_dirs(session.agent_id)
            memory_dir = str(agent_memory.agent_memory_dir(session.agent_id))

        # Fork first-turn note (session-rewind.md §5.6.4): present while
        # the fork's ephemeral fork_metadata is set (i.e. before its first
        # result clears it), so it appears on turn 1 only — framing, not
        # transcript. The replay block lives in the user channel, not here.
        fork_note: str | None = None
        if session.fork_metadata:
            try:
                fork_note = json.loads(session.fork_metadata).get("first_turn_note")
            except (json.JSONDecodeError, AttributeError):
                fork_note = None

        subagents = (agent or {}).get("subagents") or []
        if not isinstance(subagents, list):
            subagents = []

        return RunConfig(
            session_id=session.id,
            system_prompt=system_prompt,
            model=model,
            mcp_servers=mcp_servers,
            tool_allow=tool_allow,
            tool_deny=tool_deny,
            connectors=connectors or [],
            memory_dir=memory_dir,
            fork_note=fork_note,
            subagents=subagents,
        )
