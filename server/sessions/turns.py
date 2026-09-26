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
from typing import Any

from .. import fork_helpers
from .. import monitor as _monitor
from ..attachments import MAX_ATTACHMENTS_PER_MESSAGE
from ..attachments import get_path as get_attachment_path
from ..harness import HarnessEvent, HarnessRun, RunConfig, get_harness
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
            try:
                await session._backend.stop()
            except Exception:
                pass
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

    async def _run_backend(
        self, session: Session, prompt: str
    ) -> AsyncIterator[dict[str, Any]]:
        """Drive one logical turn through the backend, recovering from
        CLI premature-exit-after-tool-roundtrip if it fires.

        Each iteration of the outer loop is one CLI invocation. The
        loop normally runs exactly once and exits after a `result`
        event. If the CLI exits silently after emitting a `tool_use`
        without ever delivering `result` (the bug post-mortemed in
        docs/post-mortems/2026-05-18-bg-pipeline-hardening.md §2), we respawn it
        with the same resume id and a `"continue"` prompt to let the
        model produce the missing follow-up. Bounded by
        _MAX_RECOVERY_ATTEMPTS so a genuinely broken state can't loop.
        """

        # Load the owning agent fresh each turn — this is the live-reference
        # point: editing an agent's prompt/model/tools/MCP affects its
        # already-open sessions on their next turn (agent-refactor.md §5.2).
        agent = await self._load_agent(session)
        harness = get_harness(session.backend)
        credential = await self._resolve_credential(session, agent, harness)
        # The effective credential id (session override, else the agent's).
        # Used to flag the right row needs_reconnect on a mid-turn 401
        # (harness-credential-reauth.md §4). None = host-default CLI auth.
        cred_id = session.credential_id or (
            agent.get("credential_id") if agent else None
        )
        connectors = await self._load_connectors(agent)
        current_prompt = prompt
        recovery_attempts = 0
        transient_attempts = 0
        # A dangling resume id is recoverable exactly once per turn: clear it,
        # start fresh. A second failure is a different problem.
        stale_session_retries = 0
        # The resume id this logical turn STARTED from. A transient retry
        # re-runs the original invocation, so it must restore this — a failed
        # no-output attempt can still emit `session_started` and mutate
        # session.claude_session_id, which would otherwise turn the retry into
        # a `--resume <failed-id>` of the same prompt (Vera review,
        # harness-transient-retry.md §4).
        resume_at_turn_start = session.claude_session_id

        while True:
            # Reuse the session's live process when nothing it baked in at
            # spawn has changed (inline-steering.md §7); otherwise spawn.
            reused = self._reusable_run(
                session, session.working_dir, credential, agent, connectors
            )
            if reused is None and session._backend is not None:
                # Declining to reuse (config changed, or a turn still in
                # flight) must not simply overwrite the handle: that orphans a
                # live ~255MB process that nothing points at any more, so
                # neither the reaper nor shutdown can ever reach it. Let it go
                # properly first.
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
            saw_result = False
            saw_tool_use = False
            # Whether any assistant text streamed this attempt — gates the
            # transient retry (don't re-run a turn that already produced output).
            saw_text = False
            # Terminal-error signal for post-turn auth-expiry classification.
            saw_error_event = False
            error_event_text = ""
            # Streaming-text coalescing (inline-steering.md §4 S1). The CLI
            # emits one delta per token; forwarding each as its own WS frame
            # would be a frame per token and a React render per token. We
            # batch them into at most one frame every _DELTA_FLUSH_SECONDS,
            # which is still far below the threshold where a human sees
            # stepping, and flush before any non-delta event so ordering with
            # tool calls and the final block is exact.
            delta_buf: list[str] = []
            delta_last_flush = 0.0
            # Per-turn watchdog state (turn-safety.md §3): the watchdog stops a
            # turn that goes silent (idle) or runs too long (overall) so it can
            # never hang forever the way the deep-research wedge did.
            watchdog_state = {"last": time.monotonic(), "tripped": None}
            watchdog = self._start_turn_watchdog(backend, watchdog_state)

            try:
                if reused is not None:
                    # The process already holds this conversation, so there is
                    # no prompt to re-render and no transcript to resume: just
                    # hand it the next message.
                    await backend.send_turn(current_prompt)
                else:
                    await backend.start(
                        current_prompt,
                        session.working_dir,
                        session.claude_session_id,
                        credential=credential,
                    )

                # The steering window is open from here until `result`
                # (inline-steering.md §8). Only a backend that takes input on
                # stdin can be steered; everything else keeps queueing.
                steer_writer: asyncio.Task[int] | None = None
                if backend.reusable:
                    async with session._steer_lock:
                        session._steer_open = True
                        if session._steer_queue:
                            session._steer_ready.set()
                    steer_writer = asyncio.create_task(
                        self._steer_writer(session, backend),
                        name=f"steer-writer-{session.id}",
                    )

                async for event in backend.stream():
                    watchdog_state["last"] = time.monotonic()

                    # Coalesce token deltas; flush on a timer.
                    if event.type == "text_delta":
                        if event.content:
                            delta_buf.append(event.content)
                        now = time.monotonic()
                        if now - delta_last_flush >= _DELTA_FLUSH_SECONDS:
                            delta_last_flush = now
                            flushed = self._flush_text_deltas(session.id, delta_buf)
                            if flushed is not None:
                                yield flushed
                        continue

                    # Anything else ends the current delta run: flush it first
                    # so the partial text can never arrive after the completed
                    # block that supersedes it.
                    flushed = self._flush_text_deltas(session.id, delta_buf)
                    if flushed is not None:
                        delta_last_flush = time.monotonic()
                        yield flushed
                    # session_started arrives on the CLI's init event,
                    # before any tool work. Persist the resume id
                    # immediately so the recovery path below can use
                    # it even if the bug suppresses `result`.
                    if event.type == "session_started" and event.session_id:
                        if session.claude_session_id != event.session_id:
                            session.claude_session_id = event.session_id
                            if self.db:
                                await self.db.update_session_field(
                                    session.id, claude_session_id=event.session_id
                                )
                        # Internal event — don't persist or broadcast.
                        continue

                    # A sub-agent's progress: remembered on the session so a
                    # reload can still paint the card, broadcast so the open
                    # UI paints it now, and never persisted — the Task tool
                    # call and its result are the durable record
                    # (native-subagents.md §4).
                    if event.type == "subagent" and event.subagent is not None:
                        self._record_subagent(session, event.subagent)

                    if event.type == "tool_use":
                        saw_tool_use = True
                    if event.type == "text" and event.content and event.content.strip():
                        saw_text = True

                    # Persist whichever message shape this event maps to. The
                    # returned seq goes onto the WS event so reconnecting
                    # clients can dedupe against their snapshot.
                    msg_content = self._event_to_message_content(event)
                    msg_seq: int | None = None
                    if msg_content is not None:
                        msg_seq = await self._persist_message(session, msg_content)

                    # Track pending question state for reconnect re-render
                    if event.type == "question_request" and event.tool_use_id:
                        questions = (
                            (event.tool_input or {}).get("questions") or []
                        )
                        session._pending_questions[event.tool_use_id] = PendingQuestion(
                            question_id=event.tool_use_id,
                            questions=questions,
                        )
                        self._schedule_question_timeout(session, event.tool_use_id)

                    # Update resume id when result arrives (in case the
                    # CLI reissued a different one mid-stream).
                    if event.type == "result":
                        saw_result = True
                        _monitor.record(_MonEvent(
                            kind="turn",
                            session_id=session.id,
                            agent_id=session.agent_id,
                            backend=session.backend,
                            duration_ms=event.duration_ms,
                            ok=not event.is_error,
                            detail={
                                "cost": event.cost,
                                "num_turns": event.num_turns,
                            },
                        ))
                        # Shut the steering window first: past this point the
                        # CLI is idle, and a frame written now would start a
                        # fresh turn rather than steer this one.
                        async with session._steer_lock:
                            session._steer_open = False
                        if event.session_id and session.claude_session_id != event.session_id:
                            session.claude_session_id = event.session_id
                            if self.db:
                                await self.db.update_session_field(
                                    session.id, claude_session_id=event.session_id
                                )
                        # First fork turn produced a result: drop the ephemeral
                        # fork state so turn 2+ behaves like a normal resumed
                        # session (session-rewind.md §5.3.2/§5.6.5).
                        await self._clear_fork_first_turn_state(session)

                    # Capture terminal-error text (a failed `result` or an
                    # `error` event) for post-turn auth-expiry classification
                    # (harness-credential-reauth.md §4). tool_result errors are
                    # excluded — a tool failing isn't the turn failing.
                    if event.type in ("result", "error") and event.is_error:
                        saw_error_event = True
                        if event.content:
                            error_event_text += event.content + "\n"
                        if event.raw:
                            try:
                                error_event_text += json.dumps(event.raw) + "\n"
                            except (TypeError, ValueError):
                                pass

                    # Translate into the WS message shape the front-end expects
                    ws_event = self._event_to_ws_message(session.id, event)
                    if ws_event is not None:
                        if msg_seq is not None:
                            ws_event["seq"] = msg_seq
                        yield ws_event
            finally:
                # Close the steering window and stop the writer before
                # anything else touches the process. Whatever it didn't get to
                # write becomes a normal queued prompt — a steer that missed
                # its turn still runs, just as the next one (§9).
                async with session._steer_lock:
                    session._steer_open = False
                if steer_writer is not None and not steer_writer.done():
                    steer_writer.cancel()
                    try:
                        await steer_writer
                    except (asyncio.CancelledError, Exception):
                        pass
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

                if watchdog is not None:
                    watchdog.cancel()
                    try:
                        await watchdog
                    except (asyncio.CancelledError, Exception):
                        pass
                # Keep the process only when this turn ended the way a turn
                # is supposed to: a clean `result`, nothing tripped, nothing
                # to retry. A watchdog timeout, an error, or an interrupt all
                # leave the CLI in a state we'd rather not inherit, so those
                # shut it down and the next turn spawns fresh.
                keep = (
                    backend.reusable
                    and backend.is_alive()
                    and saw_result
                    and not saw_error_event
                    and watchdog_state["tripped"] is None
                )
                if keep:
                    session._held_run_at = time.monotonic()
                    # Enforce the cap HERE, not only on the reaper's tick.
                    # The reaper is a background task that doesn't exist in
                    # tests and runs every 30s in production, so leaving the
                    # bound to it means N finished sessions can each pin
                    # ~255MB in between — which is exactly how the backend
                    # suite got OOM-killed.
                    await self._enforce_held_cap(keep_session_id=session.id)
                else:
                    try:
                        await backend.stop()
                    except Exception:
                        logger.exception(
                            "backend.stop() failed cleanly for session %s", session.id
                        )
                    self._forget_backend(session)

            # Turn watchdog tripped (idle or overall cap): the backend was
            # stopped mid-turn. Surface a clear error and STOP — before the
            # auth/transient/premature-exit dispatch, so a timeout is never
            # mis-read as transient or respawned. turn-safety.md §3.
            if watchdog_state["tripped"] is not None:
                reason, limit = watchdog_state["tripped"]
                yield await self._surface_turn_timeout(
                    session, reason=reason, limit=limit, backend=harness.backend
                )
                return

            # Reactive auth-expiry: a failed turn whose error text (terminal
            # event content/raw + the CLI's stderr) matches this backend's
            # auth-rejection patterns means the bound credential is dead
            # (revoked / rotated / expired past what the proactive refresh
            # caught). Flag it needs_reconnect and surface a re-authorize
            # prompt, then STOP — the premature-exit "continue" respawn below
            # must not run (re-auth won't fix itself, and the retry just burns
            # the budget). harness-credential-reauth.md §4.
            turn_failed = saw_error_event or not saw_result
            if turn_failed:
                # getattr: real HarnessRun exposes stderr_text; lightweight
                # test/backend stand-ins may not.
                stderr_text = getattr(backend, "stderr_text", "") or ""
                error_blob = (error_event_text + "\n" + stderr_text)[:8000]

                # (a) Auth-credential rejection → flag + stop (never retried;
                # re-auth won't fix itself). harness-credential-reauth.md §4.
                if harness.is_auth_error(error_blob):
                    yield await self._surface_auth_expiry(
                        session, cred_id=cred_id, backend=harness.backend
                    )
                    return

                # (a2) Dangling resume id: the engine no longer holds the
                # conversation this session is pinned to (its local transcript
                # was rotated or cleaned; ours lives in the DB and is intact).
                # This is NOT transient — retrying the same id fails
                # identically forever, which bricks the session silently: a
                # result with zero turns, zero cost and no text. Drop the dead
                # id and re-run the same prompt once as a fresh engine-side
                # conversation, saying out loud that the engine lost its own
                # history so the model starts this turn without it.
                if (
                    session.claude_session_id
                    and stale_session_retries < 1
                    and harness.is_stale_session_error(error_blob)
                ):
                    stale_session_retries += 1
                    logger.warning(
                        "Session %s: resume id %s is gone from the engine; "
                        "clearing it and starting a fresh conversation",
                        session.id,
                        session.claude_session_id,
                    )
                    session.claude_session_id = None
                    resume_at_turn_start = None
                    if self.db:
                        await self.db.update_session_field(
                            session.id, claude_session_id=None
                        )
                    # Don't restart cold: Octopus still has the whole
                    # conversation (the engine's transcript is a cache of
                    # ours, not the record), so replay its tail into this
                    # turn through the same channel a fork uses when its
                    # backend can't resume natively. The model continues the
                    # conversation instead of appearing to forget it.
                    replayed = 0
                    omitted = 0
                    recovery_prompt = prompt
                    if self.db:
                        history = [
                            MessageContent(**m)
                            for m in await self.db.load_messages(session.id)
                        ]
                        kept, omitted = fork_helpers.select_lost_history(history)
                        replayed = len(kept)
                        if kept:
                            recovery_prompt = spill_if_large(
                                session.id,
                                fork_helpers.wrap_for_lost_history(
                                    prompt, kept, omitted=omitted
                                ),
                            )
                    yield await self._surface_stale_session(
                        session,
                        backend=harness.backend,
                        replayed=replayed,
                        omitted=omitted,
                    )
                    current_prompt = recovery_prompt
                    continue

                # (b) Transient provider-reliability failure (5xx / overloaded /
                # dropped connection / server-side throttle) → bounded retry.
                # TWO modes, by whether the turn already produced output:
                #   - NO output yet → re-run the ORIGINAL prompt from the
                #     turn-start resume state (side-effect-free; discard any
                #     resume id a failed no-output attempt captured — Vera).
                #   - output already streamed (tool_use/text) AND a resume id
                #     was captured → RESUME with "continue" so we pick up where
                #     it left off WITHOUT re-running tools or duplicating text.
                #     This is the common case: a long agent turn throttled
                #     mid-flight — the earlier no-output-only gate let it stop.
                # Quota/credit errors match no pattern here → surface as-is.
                # harness-transient-retry.md §4.
                if harness.is_transient_error(error_blob):
                    produced_output = saw_tool_use or saw_text
                    can_retry = (
                        transient_attempts < self._MAX_TRANSIENT_RETRIES
                        and (not produced_output or bool(session.claude_session_id))
                    )
                    if can_retry:
                        transient_attempts += 1
                        delay = self._TRANSIENT_RETRY_BASE_DELAY * (
                            2 ** (transient_attempts - 1)
                        )
                        logger.warning(
                            "Session %s: transient backend error; retrying in "
                            "%.1fs (attempt %d/%d, resume=%s)",
                            session.id, delay, transient_attempts,
                            self._MAX_TRANSIENT_RETRIES, produced_output,
                        )
                        if produced_output:
                            # Continue the in-progress conversation from its
                            # captured resume id — no re-run, no duplication.
                            current_prompt = "continue"
                        else:
                            current_prompt = prompt  # original invocation
                            if session.claude_session_id != resume_at_turn_start:
                                session.claude_session_id = resume_at_turn_start
                                if self.db:
                                    await self.db.update_session_field(
                                        session.id,
                                        claude_session_id=resume_at_turn_start,
                                    )
                        yield await self._surface_transient_retry(
                            session,
                            attempt=transient_attempts,
                            max_attempts=self._MAX_TRANSIENT_RETRIES,
                            delay=delay,
                        )
                        await asyncio.sleep(delay)
                        continue
                    # Budget exhausted (or output with no resume id to continue
                    # from) — surface a clear error so the user knows it wasn't
                    # their request that failed.
                    yield await self._surface_transient_exhausted(
                        session, backend=harness.backend, attempts=transient_attempts
                    )
                    return

            # Decide whether to recover. The bug signature is:
            # CLI exited without a `result` event AFTER emitting a
            # `tool_use`. Anything else (a clean turn, an immediate
            # crash with no tool use, a turn we've already retried
            # once) — leave it alone.
            if saw_result:
                return
            if not harness.premature_exit_recovery:
                # Harness opts out of the Claude-CLI premature-exit recovery
                # (Codex runs exactly once per turn) — codex-backend.md §5.6.
                return
            if recovery_attempts >= self._MAX_RECOVERY_ATTEMPTS:
                logger.warning(
                    "Session %s: CLI premature-exit retry budget exhausted; "
                    "giving up on this turn", session.id
                )
                return
            if not saw_tool_use:
                return
            if not session.claude_session_id:
                # No resume id captured (init never arrived) — we can't
                # respawn into the same conversation.
                return

            recovery_attempts += 1
            logger.warning(
                "Session %s: detected CLI premature-exit after tool_use; "
                "auto-respawning with 'continue' (attempt %d/%d)",
                session.id, recovery_attempts, self._MAX_RECOVERY_ATTEMPTS,
            )
            # Persist a discreet system marker so the UI / transcript
            # records that a recovery happened. Uses the same shape as
            # the (interrupted by user) marker in interrupt().
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
            yield marker_event

            current_prompt = "continue"

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
