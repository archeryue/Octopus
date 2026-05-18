from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Callable

from .backends import BackendBase, BackendCredential, BackendEvent, ClaudeCodeBackend
from .config import settings
from .crypto import decrypt
from .database import Database
from .models import MessageContent, MessageRole, SessionStatus

logger = logging.getLogger(__name__)


@dataclass
class PendingApproval:
    """Held for legacy WS approve_tool/deny_tool messages.

    The CLI-direct backend handles tool permissions itself via the control
    protocol, so we don't populate this from the new code path — it's
    retained only so existing WS clients don't get errors on the old
    message types.
    """

    tool_name: str
    tool_input: dict[str, Any]
    tool_use_id: str
    future: asyncio.Future


@dataclass
class PendingQuestion:
    """Mirror of an AskUserQuestion the backend is currently asking us.

    The backend owns the actual control-protocol future; this is just the
    info we surface to the UI so reload-on-reconnect can re-render the form.
    """

    question_id: str
    questions: list[dict[str, Any]]


@dataclass
class Session:
    id: str
    name: str
    working_dir: str
    status: SessionStatus = SessionStatus.idle
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    claude_session_id: str | None = None
    credential_id: str | None = None
    _message_count: int = field(default=0, repr=False)
    _active_task: asyncio.Task | None = field(default=None, repr=False)
    # Per-prompt task that interrupt() targets; the outer _active_task is
    # the orchestrator loop and survives interrupts so it can drain the queue.
    _inner_task: asyncio.Task | None = field(default=None, repr=False)
    _backend: BackendBase | None = field(default=None, repr=False)
    _pending_approvals: dict[str, PendingApproval] = field(default_factory=dict, repr=False)
    _pending_questions: dict[str, PendingQuestion] = field(default_factory=dict, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    _pending_queue: list[str] = field(default_factory=list, repr=False)


class SessionManager:
    def __init__(self) -> None:
        self.sessions: dict[str, Session] = {}
        self._broadcast_callbacks: dict[str, Callable] = {}
        self.db: Database | None = None
        # Wired in by main.py once the manager is constructed. Kept as
        # an opaque object — we only call `.fire(event)` on it — so the
        # session manager doesn't take a hard dependency on the
        # notifiers package's import surface.
        self._notifier_manager: Any = None

    def set_notifier_manager(self, mgr: Any) -> None:
        self._notifier_manager = mgr

    async def initialize(self, db: Database) -> None:
        self.db = db
        rows = await db.load_sessions()
        for row in rows:
            session = Session(
                id=row["id"],
                name=row["name"],
                working_dir=row["working_dir"],
                created_at=row["created_at"],
                claude_session_id=row["claude_session_id"],
                credential_id=row.get("credential_id"),
            )
            session._message_count = await db.count_messages(session.id)
            self.sessions[session.id] = session
        logger.info("Loaded %d sessions from database", len(rows))

    def on_broadcast(self, key: str, callback: Callable) -> None:
        self._broadcast_callbacks[key] = callback

    def remove_broadcast(self, key: str) -> None:
        self._broadcast_callbacks.pop(key, None)

    async def _broadcast(self, message: dict) -> None:
        for cb in list(self._broadcast_callbacks.values()):
            try:
                await cb(message)
            except Exception:
                logger.exception("Broadcast callback error")

    def list_sessions(self) -> list[Session]:
        return list(self.sessions.values())

    def get_session(self, session_id: str) -> Session | None:
        return self.sessions.get(session_id)

    async def create_session(
        self,
        name: str,
        working_dir: str | None = None,
        credential_id: str | None = None,
    ) -> Session:
        sid = uuid.uuid4().hex[:12]
        session = Session(
            id=sid,
            name=name,
            working_dir=working_dir or settings.default_working_dir,
            credential_id=credential_id,
        )
        self.sessions[sid] = session
        if self.db:
            await self.db.save_session(
                session_id=session.id,
                name=session.name,
                working_dir=session.working_dir,
                created_at=session.created_at,
                claude_session_id=session.claude_session_id,
                credential_id=session.credential_id,
            )
        return session

    async def import_session(
        self,
        name: str,
        working_dir: str | None = None,
        claude_session_id: str | None = None,
        credential_id: str | None = None,
        messages: list[MessageContent] | None = None,
    ) -> Session:
        sid = uuid.uuid4().hex[:12]
        session = Session(
            id=sid,
            name=name,
            working_dir=working_dir or settings.default_working_dir,
            claude_session_id=claude_session_id,
            credential_id=credential_id,
        )
        self.sessions[sid] = session
        if self.db:
            await self.db.save_session(
                session_id=session.id,
                name=session.name,
                working_dir=session.working_dir,
                created_at=session.created_at,
                claude_session_id=session.claude_session_id,
                credential_id=session.credential_id,
            )
        if messages:
            for msg in messages:
                await self._persist_message(session, msg)
            if self.db:
                await self.db.flush()
        return session

    async def archive_session(self, session_id: str) -> Session:
        """Hide the current session and return a fresh one with the same
        user-visible settings (name / working_dir / credential_id).

        The old session row stays in the DB (with `archived = 1`) so the
        message history isn't lost — it just disappears from the default
        sessions list. The new session starts with no `claude_session_id`
        so the CLI begins a clean conversation.

        Anything keyed off the *logical* session — schedules, bridge
        mappings — is repointed from the old id to the new one, so the
        user's automation keeps firing against the live session.

        If the old session has a running turn, it's interrupted first.
        """
        old = self.sessions.get(session_id)
        if old is None:
            raise ValueError(f"Session {session_id} not found")

        # Stop the live work, if any, before yanking the in-memory state.
        if old._inner_task and not old._inner_task.done():
            old._inner_task.cancel()
        if old._active_task and not old._active_task.done():
            old._active_task.cancel()
        if old._backend:
            try:
                await asyncio.wait_for(old._backend.stop(), timeout=2.0)
            except Exception:
                pass
            old._backend = None
        old._pending_queue.clear()
        old._pending_questions.clear()

        # Mark the DB row archived; drop it from the in-memory dict so
        # subsequent list/get calls don't surface it.
        if self.db:
            await self.db.update_session_field(session_id, archived=True)
        self.sessions.pop(session_id, None)

        # New session inherits name / working_dir / credential_id but
        # starts with no claude_session_id (fresh conversation).
        new = await self.create_session(
            name=old.name,
            working_dir=old.working_dir,
            credential_id=old.credential_id,
        )

        # Repoint anything bound to the logical session id.
        if self.db:
            await self.db.repoint_schedules(old.id, new.id)
            await self.db.repoint_bridge_mappings(old.id, new.id)

        await self._broadcast(
            {
                "type": "session_archived",
                "old_session_id": old.id,
                "new_session_id": new.id,
                "name": new.name,
            }
        )
        return new

    async def delete_session(self, session_id: str) -> bool:
        session = self.sessions.pop(session_id, None)
        if session is None:
            return False
        session._pending_queue.clear()
        session._pending_questions.clear()
        if session._inner_task and not session._inner_task.done():
            session._inner_task.cancel()
        if session._active_task and not session._active_task.done():
            session._active_task.cancel()
        if session._backend:
            try:
                await session._backend.stop()
            except Exception:
                pass
        if self.db:
            await self.db.delete_session(session_id)
        return True

    async def _persist_message(
        self, session: Session, msg: MessageContent
    ) -> int | None:
        """Persist and return the assigned seq (or None if no DB).

        Callers tag broadcast/yield events with this seq so clients can
        dedupe against the snapshot returned by GET /api/sessions/{id}
        after a reconnect.
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
        )
        return seq

    async def start_message(self, session_id: str, prompt: str) -> None:
        """Kick off a message, or queue it if the session is already running."""
        session = self.sessions.get(session_id)
        if session is None:
            raise ValueError(f"Session {session_id} not found")

        if session._active_task and not session._active_task.done():
            session._pending_queue.append(prompt)
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
            self._drive_messages(session_id, prompt)
        )

    async def _drive_messages(self, session_id: str, initial_prompt: str) -> None:
        """Run the initial prompt, then drain any queued prompts.

        Each prompt runs as an inner task that interrupt() can cancel
        independently, so cancelling one prompt doesn't stop the queue.
        """
        session = self.sessions.get(session_id)
        if session is None:
            return

        prompt: str | None = initial_prompt
        while prompt is not None:
            inner = asyncio.create_task(self._consume_message(session_id, prompt))
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
                prompt = session._pending_queue.pop(0)
                await self._broadcast(
                    {
                        "type": "dequeued",
                        "session_id": session_id,
                        "queue_length": len(session._pending_queue),
                    }
                )
            else:
                prompt = None

        # Queue is drained — fire the session-idle notifier (future-
        # features #5). Detached because notifier sends do network I/O.
        await self._fire_session_idle_notification(session)

    async def _fire_session_idle_notification(self, session: Session) -> None:
        """Notify async targets that this session just went fully idle.

        Best-effort: any failure inside a notifier is logged by the
        manager. Skipped if no manager is wired (tests, etc.).
        """
        if self._notifier_manager is None:
            return
        try:
            from .notifiers import NotifierEvent

            await self._notifier_manager.fire(
                NotifierEvent(
                    type="session_idle",
                    title=session.name or "Session idle",
                    message=(
                        f"Session '{session.name}' finished its work and is idle."
                    ),
                    session_id=session.id,
                    session_name=session.name,
                )
            )
        except Exception:
            logger.exception(
                "notifier_manager.fire raised for session %s", session.id
            )

    async def _consume_message(self, session_id: str, prompt: str) -> None:
        async for _event in self.send_message(session_id, prompt):
            pass  # send_message persists + broadcasts each event

    async def send_message(
        self, session_id: str, prompt: str
    ) -> AsyncIterator[dict[str, Any]]:
        session = self.sessions.get(session_id)
        if session is None:
            raise ValueError(f"Session {session_id} not found")

        try:
            await asyncio.wait_for(session._lock.acquire(), timeout=5.0)
        except asyncio.TimeoutError:
            raise ValueError(f"Session {session_id} is busy")

        try:
            # Record user message
            user_msg = MessageContent(
                role=MessageRole.user, type="text", content=prompt
            )
            seq = await self._persist_message(session, user_msg)
            event: dict[str, Any] = {
                "type": "user_message",
                "session_id": session_id,
                "content": prompt,
            }
            if seq is not None:
                event["seq"] = seq
            await self._broadcast(event)
            yield event

            session.status = SessionStatus.running
            await self._broadcast(
                {"type": "status", "session_id": session_id, "status": "running"}
            )

            try:
                async for ws_event in self._run_backend(session, prompt):
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
        """Cancel the currently running prompt. Queued prompts continue."""
        session = self.sessions.get(session_id)
        if session is None:
            return False
        inner = session._inner_task
        if inner is None or inner.done():
            return False

        # Cancel the task FIRST — synchronous and immediate. The backend's
        # stop() can take a moment (subprocess teardown), and if we awaited
        # it before cancelling, the WS handler would stay stuck inside
        # this coroutine and refuse subsequent interrupt requests.
        inner.cancel()

        # Then best-effort interrupt, with a tight timeout so a slow
        # subprocess teardown can't wedge the WS receive loop.
        if session._backend:
            try:
                await asyncio.wait_for(session._backend.interrupt(), timeout=2.0)
            except Exception:
                pass

        session._pending_questions.clear()

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
            session._backend = None
        if session._lock.locked():
            session._lock.release()
        session.status = SessionStatus.idle
        session._pending_approvals.clear()
        session._pending_questions.clear()
        await self._broadcast(
            {"type": "status", "session_id": session_id, "status": "idle"}
        )

    # ------------------------------------------------------------------ backend run loop

    async def _run_backend(
        self, session: Session, prompt: str
    ) -> AsyncIterator[dict[str, Any]]:
        """Drive one turn through the configured backend, translating each
        BackendEvent into a (persist, broadcast) pair."""

        backend = self._make_backend(session)
        session._backend = backend

        credential = await self._resolve_credential(session)

        try:
            await backend.start(
                prompt,
                session.working_dir,
                session.claude_session_id,
                credential=credential,
            )

            async for event in backend.stream():
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

                # Update resume id when result arrives
                if event.type == "result":
                    if event.session_id:
                        session.claude_session_id = event.session_id
                        if self.db:
                            await self.db.update_session_field(
                                session.id, claude_session_id=event.session_id
                            )

                # Translate into the WS message shape the front-end expects
                ws_event = self._event_to_ws_message(session.id, event)
                if ws_event is not None:
                    if msg_seq is not None:
                        ws_event["seq"] = msg_seq
                    yield ws_event
        finally:
            try:
                await backend.stop()
            except Exception:
                logger.exception("backend.stop() failed cleanly for session %s", session.id)
            session._backend = None

    def _make_backend(self, session: Session) -> BackendBase:
        """Instantiate the backend for a session. Currently only Claude Code.

        Future: dispatch on `session.backend` field ("claude-code" | "codex").
        """
        return ClaudeCodeBackend()

    async def _resolve_credential(self, session: Session) -> BackendCredential | None:
        """Look up the session's credential and decrypt the secret.

        Returns None when no credential is attached — the backend then falls
        back to whatever auth the CLI finds in its own config.
        """
        if not session.credential_id or self.db is None:
            return None
        row = await self.db.get_credential(session.credential_id)
        if row is None:
            logger.warning(
                "Session %s references missing credential %s; running without auth override",
                session.id,
                session.credential_id,
            )
            return None
        try:
            plaintext = decrypt(row["secret_encrypted"], settings.auth_token)
        except ValueError:
            logger.warning(
                "Could not decrypt credential %s (wrong auth token?); running without auth override",
                session.credential_id,
            )
            return None
        return BackendCredential(
            backend=row["backend"],
            auth_type=row["auth_type"],
            secret=plaintext,
        )

    # ------------------------------------------------------------------ event translation

    @staticmethod
    def _event_to_message_content(event: BackendEvent) -> MessageContent | None:
        if event.type == "text":
            if not event.content or not event.content.strip():
                return None
            return MessageContent(
                role=MessageRole.assistant, type="text", content=event.content
            )
        if event.type == "thinking":
            # Persist thinking as a typed message; the UI can choose to hide
            # it. Don't filter at the persistence layer.
            return MessageContent(
                role=MessageRole.assistant,
                type="thinking",
                content=event.content,
            )
        if event.type == "tool_use":
            return MessageContent(
                role=MessageRole.assistant,
                type="tool_use",
                tool_name=event.tool_name,
                tool_input=event.tool_input,
                tool_use_id=event.tool_use_id,
            )
        if event.type == "tool_result":
            return MessageContent(
                role=MessageRole.tool,
                type="tool_result",
                content=event.content,
                tool_use_id=event.tool_use_id,
                is_error=event.is_error,
            )
        if event.type == "question_request":
            return MessageContent(
                role=MessageRole.assistant,
                type="question_request",
                tool_name="AskUserQuestion",
                tool_input=event.tool_input,
                tool_use_id=event.tool_use_id,
            )
        if event.type == "result":
            return MessageContent(
                role=MessageRole.system,
                type="result",
                session_id=event.session_id,
                cost=event.cost,
            )
        return None

    @staticmethod
    def _event_to_ws_message(session_id: str, event: BackendEvent) -> dict[str, Any] | None:
        if event.type == "text":
            if not event.content or not event.content.strip():
                return None
            return {
                "type": "assistant_text",
                "session_id": session_id,
                "content": event.content,
            }
        if event.type == "thinking":
            # We persist thinking but don't broadcast it by default — the
            # UI doesn't render it today.
            return None
        if event.type == "tool_use":
            return {
                "type": "tool_use",
                "session_id": session_id,
                "tool": event.tool_name,
                "input": event.tool_input,
                "tool_use_id": event.tool_use_id,
            }
        if event.type == "tool_result":
            return {
                "type": "tool_result",
                "session_id": session_id,
                "tool_use_id": event.tool_use_id,
                "output": event.content,
                "is_error": event.is_error,
            }
        if event.type == "question_request":
            return {
                "type": "question_request",
                "session_id": session_id,
                "question_id": event.tool_use_id,
                "questions": (event.tool_input or {}).get("questions") or [],
            }
        if event.type == "result":
            return {
                "type": "result",
                "session_id": session_id,
                "claude_session_id": event.session_id,
                "cost": event.cost,
                "turns": event.num_turns,
                "duration_ms": event.duration_ms,
                "is_error": event.is_error,
            }
        return None

    # ------------------------------------------------------------------ Q&A wiring

    async def answer_question(
        self,
        session_id: str,
        question_id: str,
        answers: list[dict[str, Any]],
    ) -> bool:
        session = self.sessions.get(session_id)
        if not session or session._backend is None:
            return False
        pending = session._pending_questions.get(question_id)
        if pending is None:
            return False
        answer_text = self._format_answers(pending.questions, answers)
        ok = await session._backend.answer_question(question_id, answer_text)
        if not ok:
            return False
        # Record the answer in history and notify the UI
        ans_msg = MessageContent(
            role=MessageRole.user,
            type="question_answer",
            tool_use_id=question_id,
            content=answer_text,
        )
        ans_seq = await self._persist_message(session, ans_msg)
        event: dict[str, Any] = {
            "type": "question_answer",
            "session_id": session_id,
            "question_id": question_id,
            "content": answer_text,
        }
        if ans_seq is not None:
            event["seq"] = ans_seq
        await self._broadcast(event)
        session._pending_questions.pop(question_id, None)
        return True

    @staticmethod
    def _format_answers(
        questions: list[dict[str, Any]], answers: list[dict[str, Any]]
    ) -> str:
        """Render the user's answers as a string Claude can read.

        `answers` is a list aligned with `questions`; each entry has
        either {"selected": [labels]} or {"text": "free-form"}.
        """
        lines: list[str] = []
        for i, q in enumerate(questions):
            question_text = q.get("question", "")
            ans = answers[i] if i < len(answers) else {}
            if ans.get("text"):
                lines.append(f"Q: {question_text}\nA: {ans['text']}")
            else:
                selected = ans.get("selected") or []
                if isinstance(selected, str):
                    selected = [selected]
                lines.append(
                    f"Q: {question_text}\nA: {', '.join(selected) if selected else '(no answer)'}"
                )
        return "\n\n".join(lines)

    # ------------------------------------------------------------------ legacy tool approval (no-op surface)

    async def approve_tool(self, session_id: str, tool_use_id: str) -> bool:
        """Legacy SDK-era hook. The CLI-direct backend handles tool
        permissions internally, so this is effectively a no-op."""
        session = self.sessions.get(session_id)
        if not session:
            return False
        pending = session._pending_approvals.get(tool_use_id)
        if not pending or pending.future.done():
            return False
        pending.future.set_result(True)
        return True

    async def deny_tool(
        self, session_id: str, tool_use_id: str, reason: str = ""
    ) -> bool:
        """Legacy SDK-era hook. The CLI-direct backend handles tool
        permissions internally, so this is effectively a no-op."""
        session = self.sessions.get(session_id)
        if not session:
            return False
        pending = session._pending_approvals.get(tool_use_id)
        if not pending or pending.future.done():
            return False
        pending.future.set_result(False)
        return True


# Singleton
session_manager = SessionManager()
