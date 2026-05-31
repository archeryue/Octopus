from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Callable

from .attachments import (
    MAX_ATTACHMENTS_PER_MESSAGE,
    AttachmentError,
    delete_session_attachments,
    get_path as get_attachment_path,
)
from .large_prompts import (
    delete_session_large_prompts,
    spill_if_large,
)
from .harness import (
    HarnessCredential,
    HarnessEvent,
    HarnessRun,
    RunConfig,
    get_harness,
)
from .config import settings
from .crypto import decrypt, encrypt
from .database import Database
from .oauth_errors import RefreshErrorCode
from .oauth_providers import OAuthTokenSet, get_provider
from .models import (
    AttachmentMetadata,
    MessageContent,
    MessageRole,
    PendingQuestionInfo,
    SessionDetail,
    SessionStatus,
)

logger = logging.getLogger(__name__)


def resolve_working_dir(working_dir: str | None) -> str:
    """Freeze a session's working directory to an ABSOLUTE path at creation.

    A session's conversation + memory live under a path Claude derives from
    its working directory (``projects/<cwd-slug>/``). If we stored the dir
    relative (``.``, ``Octopus``), the slug would be re-resolved against the
    *server process's* cwd on every turn — so the storage location would
    depend on where/how the server happens to be launched. That ambient
    coupling silently relocates (and orphans) a session's history whenever
    the server's cwd differs: a manual launch from another dir, an edited
    systemd unit, or a cloud deployment with a different pwd.

    Resolving to absolute once, here, removes ``os.getcwd()`` from the
    equation forever after: the slug becomes a pure function of session-owned
    data. Relative input is interpreted against the server cwd this one time
    (the natural meaning of a path the caller typed), then frozen.
    """
    raw = working_dir or settings.default_working_dir
    return str(Path(raw).expanduser().resolve())


@dataclass
class QueuedPrompt:
    """A user turn waiting to run.

    Carries both the raw prompt text and any attachments the user
    uploaded with it — we resolve attachments → absolute paths only at
    spawn time (not at enqueue time) so the agent sees the same prompt
    shape regardless of whether the turn ran immediately or after a
    queue drain.
    """

    prompt: str
    attachment_ids: list[str]


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
    # Owning agent (agent-refactor.md). agent_id is required for any session
    # created post-refactor; left optional on the dataclass so legacy
    # in-memory construction paths don't break mid-migration.
    agent_id: str | None = None
    # Who created this session: 'user' | 'schedule' | 'bridge' | 'delegation'.
    # Scheduler fires auto-archive on idle (§5.6); bridge/user sessions
    # persist. 'delegation' sessions auto-archive on idle too — they're a
    # transient child spawned by an agent-to-agent ask_agent call
    # (agent-collaboration.md §5.2).
    origin: str = "user"
    # Which AI backend drives this session ('claude-code' | 'codex').
    backend: str = "claude-code"
    # Agent-to-agent: parent session that spawned this delegation, or None
    # for every non-delegation session. Used by the delegation listener to
    # route replies/questions/errors back to the parent and by guards to
    # walk the caller chain (cycle + depth). (agent-collaboration.md §4.1)
    parent_session_id: str | None = None
    # The original delegation prompt, kept verbatim for UI display on
    # delegation sessions. NULL elsewhere.
    delegation_request: str | None = None
    _message_count: int = field(default=0, repr=False)
    _active_task: asyncio.Task | None = field(default=None, repr=False)
    # Per-prompt task that interrupt() targets; the outer _active_task is
    # the orchestrator loop and survives interrupts so it can drain the queue.
    _inner_task: asyncio.Task | None = field(default=None, repr=False)
    _backend: HarnessRun | None = field(default=None, repr=False)
    _pending_approvals: dict[str, PendingApproval] = field(default_factory=dict, repr=False)
    _pending_questions: dict[str, PendingQuestion] = field(default_factory=dict, repr=False)
    # question_id -> background timer that auto-answers if the user
    # never replies (see SessionManager._schedule_question_timeout).
    _question_timers: dict[str, asyncio.Task] = field(default_factory=dict, repr=False)
    # AUQ delivery coordination for the new MCP-based flow. The
    # `mcp__ask__user` tool (server/mcp_servers/ask.py) creates a
    # pending question via REST, then HTTP-long-polls the answer
    # endpoint, which awaits the Event below. The user's UI submit
    # sets `_pending_question_answers[q_id]` and signals the Event;
    # the long-poll unblocks and returns the answer to the MCP tool,
    # which returns it as the tool result so the model can continue.
    # Replaces the old --permission-prompt-tool=stdio deny-channel
    # hack that exposed us to the CLI's premature-exit bug.
    _pending_question_events: dict[str, asyncio.Event] = field(default_factory=dict, repr=False)
    _pending_question_answers: dict[str, str] = field(default_factory=dict, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    _pending_queue: list[QueuedPrompt] = field(default_factory=list, repr=False)


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
        # Likewise wired in by main.py — the ScheduleRunner. Used to
        # re-register jobs when archiving a session repoints the schedules
        # anchored to it. Opaque: we only call `.reschedule(row)`.
        self._schedule_runner: Any = None

    def set_notifier_manager(self, mgr: Any) -> None:
        self._notifier_manager = mgr

    def set_schedule_runner(self, runner: Any) -> None:
        self._schedule_runner = runner

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
                agent_id=row.get("agent_id"),
                origin=row.get("origin") or "user",
                backend=row.get("backend") or "claude-code",
                parent_session_id=row.get("parent_session_id"),
                delegation_request=row.get("delegation_request"),
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
        agent_id: str,
        name: str | None = None,
        working_dir: str | None = None,
        credential_id: str | None = None,
        origin: str = "user",
        backend: str = "claude-code",
        parent_session_id: str | None = None,
        delegation_request: str | None = None,
    ) -> Session:
        """Create a conversation thread owned by `agent_id`.

        A session is *an instance of talking to an agent* (agent-refactor.md
        §5.2). Refuses a missing/unknown agent. `working_dir` defaults to
        `settings.default_working_dir` — agents are not path-aware. `name`
        defaults to a generated "{agent} — {timestamp}" label.

        `parent_session_id` + `delegation_request` are set together for
        agent-to-agent delegation children (agent-collaboration.md §4.1):
        the child carries a pointer back to the parent session and a
        verbatim copy of the original delegation prompt for UI display.
        """
        if not agent_id:
            raise ValueError("agent_id is required to create a session")
        agent = await self.db.get_agent(agent_id) if self.db else None
        if self.db and agent is None:
            raise ValueError(f"Agent {agent_id} not found")

        if not name:
            label = (agent or {}).get("name", "Agent")
            stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
            name = f"{label} — {stamp}"

        sid = uuid.uuid4().hex[:12]
        session = Session(
            id=sid,
            name=name,
            working_dir=resolve_working_dir(working_dir),
            credential_id=credential_id,
            agent_id=agent_id,
            origin=origin,
            backend=backend,
            parent_session_id=parent_session_id,
            delegation_request=delegation_request,
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
                agent_id=session.agent_id,
                origin=session.origin,
                backend=session.backend,
                parent_session_id=session.parent_session_id,
                delegation_request=session.delegation_request,
            )
        return session

    async def import_session(
        self,
        name: str,
        working_dir: str | None = None,
        claude_session_id: str | None = None,
        credential_id: str | None = None,
        messages: list[MessageContent] | None = None,
        agent_id: str | None = None,
        origin: str = "user",
        backend: str = "claude-code",
    ) -> Session:
        sid = uuid.uuid4().hex[:12]
        session = Session(
            id=sid,
            name=name,
            working_dir=resolve_working_dir(working_dir),
            claude_session_id=claude_session_id,
            credential_id=credential_id,
            agent_id=agent_id,
            origin=origin,
            backend=backend,
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
                agent_id=session.agent_id,
                origin=session.origin,
                backend=session.backend,
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
        so the CLI begins a clean conversation, under the same agent.

        Schedules and bridges are owned by the *Agent* now, not the
        session, so there is nothing to repoint (agent-refactor.md §5.2).
        The only bridge-aware step: if this session was some chat's sticky
        pointer, null it so the next inbound message opens a fresh thread.

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
        self._cancel_all_question_timers(old)

        # Mark the DB row archived; drop it from the in-memory dict so
        # subsequent list/get calls don't surface it.
        if self.db:
            await self.db.update_session_field(session_id, archived=True)
        self.sessions.pop(session_id, None)

        # New session inherits agent / name / working_dir / credential_id /
        # origin but starts with no claude_session_id (fresh conversation).
        agent_id = old.agent_id
        if agent_id is None and self.db:
            sys_agent = await self.db.get_system_agent()
            agent_id = sys_agent["id"] if sys_agent else None
        new = await self.create_session(
            agent_id=agent_id,
            name=old.name,
            working_dir=old.working_dir,
            credential_id=old.credential_id,
            origin=old.origin,
            backend=old.backend,
        )

        # Schedules/bridges are agent-owned, so ownership needs no repoint.
        # But a schedule created from this session (origin_session_id == old.id)
        # should follow the live successor thread, otherwise its runs fall back
        # to throwaway sessions. Move them onto `new`, then re-register the live
        # jobs so the next fire targets the successor, not the archived session.
        # Also clear any sticky bridge pointer aimed at the old session so the
        # next inbound message opens a fresh thread.
        if self.db:
            repointed = await self.db.repoint_schedules_origin(old.id, new.id)
            if self._schedule_runner is not None:
                for row in repointed:
                    await self._schedule_runner.reschedule(row)
            await self.db.clear_bridge_sticky_for_session(old.id)

        await self._broadcast(
            {
                "type": "session_archived",
                "old_session_id": old.id,
                "new_session_id": new.id,
                "name": new.name,
            }
        )
        return new

    # Session origins whose sessions should be archived once they go
    # fully idle from the generic idle hook, so they don't pile up the
    # active list. Only 'schedule' is auto-archived from idle —
    # delegation children must NOT be, because a delegation parent
    # that has fired an outbound `ask_agent` is idle while waiting
    # for its child to reply (see agent-collaboration.md §5.2 nested
    # chain). If we archive it here, the child's terminal injection
    # later targets a missing parent and is silently dropped. Instead,
    # delegation children are archived by DelegationManager
    # ._inject_terminal once their own terminal turn has actually been
    # delivered — that's when the chain work is genuinely done.
    _AUTO_ARCHIVE_ORIGINS = ("schedule",)
    # The widened set the auto_archive helper itself accepts. Callers
    # outside the idle hook (e.g. DelegationManager._inject_terminal)
    # can still archive delegation sessions — but only at the right
    # moment, not on every idle transition.
    _AUTO_ARCHIVE_ELIGIBLE = ("schedule", "delegation")

    async def auto_archive_scheduled_session(self, session_id: str) -> bool:
        """Hide a finished transient session (schedule or delegation
        child) once it goes idle. agent-refactor.md §5.6 + agent-
        collaboration.md §5.2.

        Unlike `archive_session`, no replacement thread is created — the
        next fire (schedule) or delegation request materializes its own
        fresh session under the agent. No-op if the session is gone,
        not auto-archivable by origin, or still running. Returns True
        if it archived.

        The function name is kept (rather than renamed) because it's
        referenced from main.py / scheduler.py; the behaviour
        generalises while the call sites stay stable.
        """
        session = self.sessions.get(session_id)
        if (
            session is None
            or session.origin not in self._AUTO_ARCHIVE_ELIGIBLE
        ):
            return False
        if session._active_task and not session._active_task.done():
            return False  # still working — don't yank it
        if self.db:
            await self.db.update_session_field(session_id, archived=True)
            await self.db.clear_bridge_sticky_for_session(session_id)
        self.sessions.pop(session_id, None)
        await self._broadcast(
            {
                "type": "session_archived",
                "old_session_id": session_id,
                "new_session_id": None,
                "name": session.name,
            }
        )
        return True

    async def evict_agent_sessions(self, agent_id: str) -> list[str]:
        """Drop all live sessions owned by an agent from the in-memory map
        (used when the agent is archived — the DB rows are already flagged
        archived by `db.archive_agent`). Stops any running turn first.
        Returns the evicted session ids.
        """
        evicted: list[str] = []
        for sid, session in list(self.sessions.items()):
            if session.agent_id != agent_id:
                continue
            if session._inner_task and not session._inner_task.done():
                session._inner_task.cancel()
            if session._active_task and not session._active_task.done():
                session._active_task.cancel()
            if session._backend:
                try:
                    await asyncio.wait_for(session._backend.stop(), timeout=2.0)
                except Exception:
                    pass
                session._backend = None
            session._pending_queue.clear()
            self._cancel_all_question_timers(session)
            self.sessions.pop(sid, None)
            evicted.append(sid)
            await self._broadcast(
                {
                    "type": "session_archived",
                    "old_session_id": sid,
                    "new_session_id": None,
                    "name": session.name,
                }
            )
        return evicted

    async def list_archived_sessions(self) -> list[dict[str, Any]]:
        """Return SessionInfo-shaped dicts for every archived DB row.

        Pulled lazily from the DB (archived sessions aren't kept in the
        in-memory `self.sessions` map). Caller turns them into Pydantic
        models for the response.
        """
        if self.db is None:
            return []
        rows = await self.db.load_sessions(include_archived=True)
        out: list[dict[str, Any]] = []
        for row in rows:
            if not row["archived"]:
                continue
            count = await self.db.count_messages(row["id"])
            out.append(
                {
                    "id": row["id"],
                    "name": row["name"],
                    "working_dir": row["working_dir"],
                    "status": SessionStatus.idle.value,
                    "created_at": row["created_at"],
                    "message_count": count,
                    "claude_session_id": row["claude_session_id"],
                    "credential_id": row.get("credential_id"),
                    "agent_id": row.get("agent_id"),
                    "origin": row.get("origin") or "user",
                    "backend": row.get("backend") or "claude-code",
                    "parent_session_id": row.get("parent_session_id"),
                    "delegation_request": row.get("delegation_request"),
                    "archived": True,
                }
            )
        return out

    async def load_archived_session_detail(
        self, session_id: str
    ) -> SessionDetail | None:
        """Read full message history for an archived session straight
        from the DB. Returns None if the id isn't an archived row.
        """
        if self.db is None:
            return None
        rows = await self.db.load_sessions(include_archived=True)
        match = next(
            (r for r in rows if r["id"] == session_id and r["archived"]), None
        )
        if match is None:
            return None
        messages_raw = await self.db.load_messages(session_id)
        messages = [MessageContent(**m) for m in messages_raw]
        return SessionDetail(
            id=match["id"],
            name=match["name"],
            working_dir=match["working_dir"],
            status=SessionStatus.idle,
            created_at=match["created_at"],
            message_count=len(messages),
            claude_session_id=match["claude_session_id"],
            credential_id=match.get("credential_id"),
            agent_id=match.get("agent_id"),
            origin=match.get("origin") or "user",
            backend=match.get("backend") or "claude-code",
            parent_session_id=match.get("parent_session_id"),
            delegation_request=match.get("delegation_request"),
            archived=True,
            messages=messages,
            pending_queue=[],
            pending_questions=[],
            next_message_seq=len(messages),
        )

    async def unarchive_session(self, session_id: str) -> Session:
        """Flip archived=0 in the DB and reload the row into memory.

        Refuses unknown / non-archived ids with ValueError.
        """
        if self.db is None:
            raise ValueError("DB not initialized")
        rows = await self.db.load_sessions(include_archived=True)
        match = next(
            (r for r in rows if r["id"] == session_id and r["archived"]), None
        )
        if match is None:
            raise ValueError(f"Archived session {session_id} not found")
        await self.db.update_session_field(session_id, archived=False)
        # Reload into the in-memory map so writes (sendMessage etc.)
        # immediately route to this session.
        session = Session(
            id=match["id"],
            name=match["name"],
            working_dir=match["working_dir"],
            created_at=match["created_at"],
            claude_session_id=match["claude_session_id"],
            credential_id=match.get("credential_id"),
            agent_id=match.get("agent_id"),
            origin=match.get("origin") or "user",
            backend=match.get("backend") or "claude-code",
            # Preserve the delegation chain fields when unarchiving —
            # without these, an unarchived delegation child would lose
            # its parent_session_id pointer and the "Delegated from"
            # banner / cycle walk would break.
            parent_session_id=match.get("parent_session_id"),
            delegation_request=match.get("delegation_request"),
        )
        session._message_count = await self.db.count_messages(session.id)
        self.sessions[session.id] = session
        await self._broadcast(
            {
                "type": "session_unarchived",
                "session_id": session.id,
                "name": session.name,
            }
        )
        return session

    async def delete_session(self, session_id: str) -> bool:
        session = self.sessions.pop(session_id, None)
        if session is None:
            return False
        session._pending_queue.clear()
        session._pending_questions.clear()
        self._cancel_all_question_timers(session)
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
        # Best-effort: wipe any uploaded files for this session. We do
        # this after the DB delete so the FK cascade has already removed
        # message rows pointing at them — if rmtree fails, the session
        # row is still gone, which is the user-visible expectation.
        delete_session_attachments(session_id)
        delete_session_large_prompts(session_id)
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
            attachments=[a.model_dump() for a in msg.attachments] if msg.attachments else None,
        )
        return seq

    async def start_message(
        self,
        session_id: str,
        prompt: str,
        attachment_ids: list[str] | None = None,
    ) -> None:
        """Kick off a message, or queue it if the session is already running.

        `attachment_ids` are previously-uploaded files (see
        `POST /api/sessions/{id}/attachments`). They're carried with the
        prompt through the queue and resolved to absolute paths at spawn
        time so the agent's `Read` tool can open them.
        """
        session = self.sessions.get(session_id)
        if session is None:
            raise ValueError(f"Session {session_id} not found")

        if attachment_ids and len(attachment_ids) > MAX_ATTACHMENTS_PER_MESSAGE:
            raise ValueError(
                f"too many attachments: max {MAX_ATTACHMENTS_PER_MESSAGE}"
            )

        queued = QueuedPrompt(prompt=prompt, attachment_ids=list(attachment_ids or []))

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
        from .bg_tasks import render_delivery_prompt

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
        except asyncio.TimeoutError:
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

            # Record user message — content is the *raw* prompt the user
            # typed; the augmented `<attachments>` block is only what we
            # hand to the backend.
            user_msg = MessageContent(
                role=MessageRole.user,
                type="text",
                content=prompt,
                attachments=attachments_meta,
            )
            seq = await self._persist_message(session, user_msg)
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
        self._cancel_all_question_timers(session)
        await self._broadcast(
            {"type": "status", "session_id": session_id, "status": "idle"}
        )

    # ------------------------------------------------------------------ backend run loop

    # Max number of auto-respawn recoveries per logical turn. The CLI
    # bug we're recovering from (post-mortem in
    # docs/post-mortems/2026-05-18-bg-pipeline-hardening.md §2) is bursty but not
    # endlessly recurring on the same turn — one retry is the sweet
    # spot between "rescue the common case" and "don't loop forever
    # burning tokens on a genuinely broken state."
    _MAX_RECOVERY_ATTEMPTS = 1

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
        connectors = await self._load_connectors(agent)
        current_prompt = prompt
        recovery_attempts = 0

        while True:
            backend = self._make_run(session, agent, connectors)
            session._backend = backend
            saw_result = False
            saw_tool_use = False

            try:
                await backend.start(
                    current_prompt,
                    session.working_dir,
                    session.claude_session_id,
                    credential=credential,
                )

                async for event in backend.stream():
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

                    if event.type == "tool_use":
                        saw_tool_use = True

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
                        if event.session_id and session.claude_session_id != event.session_id:
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
                    logger.exception(
                        "backend.stop() failed cleanly for session %s", session.id
                    )
                session._backend = None

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
        from .connectors.base import ConnectorInstallation
        from .connectors.custom import resolve_connector

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
        return get_harness(session.backend).create_run(
            self._run_config(session, agent, connectors)
        )

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
            from . import agent_memory

            agent_memory.ensure_agent_dirs(session.agent_id)
            memory_dir = str(agent_memory.agent_memory_dir(session.agent_id))

        return RunConfig(
            session_id=session.id,
            system_prompt=system_prompt,
            model=model,
            mcp_servers=mcp_servers,
            tool_allow=tool_allow,
            tool_deny=tool_deny,
            connectors=connectors or [],
            memory_dir=memory_dir,
        )

    # Refresh the access_token if it expires within this many seconds. A
    # 5-minute pad covers a slow turn that crosses the boundary without
    # forcing a refresh on every spawn.
    _OAUTH_REFRESH_LEEWAY_SEC = 300

    async def _resolve_credential(
        self, session: Session, agent: dict[str, Any] | None, harness
    ) -> HarnessCredential | None:
        """Look up the effective credential and resolve it for `harness`.

        Effective id is `session.credential_id` if set, else the agent's
        (agent-refactor.md §5.2 / decision #2). The harness's profile
        `credential_style` picks the shape: `env_secret` decrypts the secret
        (refreshing an OAuth bundle if near expiry); `home_dir` resolves the
        CODEX_HOME directory. Returns None when nothing's attached / resolvable.
        """
        cred_id = session.credential_id or (
            agent.get("credential_id") if agent else None
        )
        return await self.resolve_credential_by_id(
            cred_id, style=harness.profile.credential_style, context=f"session {session.id}"
        )

    async def resolve_credential_by_id(
        self, cred_id: str | None, *, style: str = "env_secret", context: str = ""
    ) -> HarnessCredential | None:
        """Resolve a credential id into a `HarnessCredential` of the shape the
        harness needs (`style`). Returns None when there's no id, the row is
        missing/needs_reconnect, or it can't be resolved — the caller then runs
        with whatever auth the CLI finds on its own. `context` labels log lines.

        - ``home_dir`` (Codex): the credential is directory-backed; its dir is
          deterministic (`<codex_home_dir>/<credential_id>/`), so we resolve it
          with no DB read and require a completed login (auth.json present).
        - ``env_secret`` (Claude): decrypt the secret; refresh an OAuth bundle
          if near expiry, else use the long-lived key as-is.
        """
        if not cred_id:
            return None

        if style == "home_dir":
            from .codex_login import codex_home_for

            home = codex_home_for(cred_id)
            if not os.path.exists(os.path.join(home, "auth.json")):
                return None  # inherit the host default ~/.codex (option A)
            return HarnessCredential(backend="codex", auth_type="oauth", home_dir=home)

        if self.db is None:
            return None
        row = await self.db.get_credential(cred_id)
        if row is None:
            logger.warning(
                "%s references missing credential %s; running without auth override",
                context or "caller",
                cred_id,
            )
            return None
        if row.get("needs_reconnect"):
            logger.warning(
                "Credential %s is in needs_reconnect state (%s); running without auth override",
                cred_id,
                row.get("last_refresh_error_code"),
            )
            return None
        try:
            plaintext = decrypt(row["secret_encrypted"], settings.auth_token)
        except ValueError:
            logger.warning(
                "Could not decrypt credential %s (wrong auth token?); running without auth override",
                cred_id,
            )
            return None

        # OAuth-token bundle (Pro/Max subscriber path): the secret is a
        # JSON blob, not a bare key. Refresh if close to expiry, then use
        # the access_token as the runtime secret.
        if row["auth_type"] == "oauth" and plaintext.startswith("{"):
            access_token = await self._refresh_oauth_if_needed(
                credential_id=cred_id,
                backend=row["backend"],
                bundle_json=plaintext,
            )
            if access_token is None:
                return None
            return HarnessCredential(
                backend=row["backend"],
                auth_type="oauth",
                secret=access_token,
            )

        # Either auth_type=api_key OR legacy auth_type=oauth where the
        # stored secret is the long-lived sk-ant- key from mint_api_key.
        # Both flow through ANTHROPIC_API_KEY at the backend.
        return HarnessCredential(
            backend=row["backend"],
            auth_type="api_key",
            secret=plaintext,
        )

    async def _refresh_oauth_if_needed(
        self,
        *,
        credential_id: str,
        backend: str,
        bundle_json: str,
    ) -> str | None:
        """Return a usable access_token for an OAuth-bundle credential.

        Parses the stored bundle. If the access_token is still fresh,
        returns it as-is. Otherwise hits the provider's refresh endpoint,
        persists the new bundle (DB write), and returns the new
        access_token.

        On unrecoverable refresh failure (refresh_token expired/reused/etc),
        marks the credential needs_reconnect with the right error code so
        the frontend can prompt re-login, and returns None.
        """
        try:
            bundle = json.loads(bundle_json)
        except json.JSONDecodeError:
            logger.warning(
                "Credential %s: stored OAuth bundle isn't valid JSON",
                credential_id,
            )
            return None

        access_token = bundle.get("access_token")
        refresh_token = bundle.get("refresh_token")
        expires_at_epoch = bundle.get("expires_at_epoch", 0)
        if not isinstance(access_token, str):
            logger.warning(
                "Credential %s: OAuth bundle missing access_token",
                credential_id,
            )
            return None

        if (
            isinstance(expires_at_epoch, (int, float))
            and expires_at_epoch - time.time() > self._OAUTH_REFRESH_LEEWAY_SEC
        ):
            return access_token

        if not isinstance(refresh_token, str) or not refresh_token:
            # Can't refresh — mark needs_reconnect so the user knows.
            await self._mark_needs_reconnect(
                credential_id, RefreshErrorCode.refresh_token_other
            )
            return None

        try:
            provider = get_provider(backend)
        except KeyError:
            logger.warning(
                "Credential %s: unknown backend %r, can't refresh",
                credential_id,
                backend,
            )
            return None

        try:
            new_ts: OAuthTokenSet = await provider.refresh_access_token(refresh_token)
        except RuntimeError as e:
            code = self._classify_refresh_error(str(e))
            logger.warning(
                "Credential %s: refresh failed (%s): %s", credential_id, code.value, e
            )
            await self._mark_needs_reconnect(credential_id, code)
            return None
        except Exception:
            logger.exception(
                "Credential %s: unexpected refresh error", credential_id
            )
            await self._mark_needs_reconnect(
                credential_id, RefreshErrorCode.unknown
            )
            return None

        new_bundle = {
            "access_token": new_ts.access_token,
            "refresh_token": new_ts.refresh_token,
            "expires_at_epoch": new_ts.expires_at_epoch,
            "scopes": list(new_ts.scopes),
            "token_type": new_ts.token_type,
        }
        secret_encrypted = encrypt(
            json.dumps(new_bundle, separators=(",", ":")),
            settings.auth_token,
        )
        token_expires_at = datetime.fromtimestamp(
            new_ts.expires_at_epoch, tz=timezone.utc
        ).isoformat()
        await self.db.update_credential(
            credential_id,
            secret_encrypted=secret_encrypted,
            token_expires_at=token_expires_at,
            needs_reconnect=False,
            last_refresh_error_code=None,
        )
        return new_ts.access_token

    async def _mark_needs_reconnect(
        self, credential_id: str, code: RefreshErrorCode
    ) -> None:
        if self.db is None:
            return
        await self.db.update_credential(
            credential_id,
            needs_reconnect=True,
            last_refresh_error_code=code.value,
        )

    @staticmethod
    def _classify_refresh_error(msg: str) -> RefreshErrorCode:
        lower = msg.lower()
        if "expired" in lower:
            return RefreshErrorCode.refresh_token_expired
        if "reused" in lower or "already used" in lower:
            return RefreshErrorCode.refresh_token_reused
        if "invalid_grant" in lower or "invalidated" in lower or "revoked" in lower:
            return RefreshErrorCode.refresh_token_invalidated
        if (
            "network" in lower
            or "timeout" in lower
            or "connection" in lower
        ):
            return RefreshErrorCode.network_error
        if "refresh endpoint returned" in lower:
            return RefreshErrorCode.refresh_token_other
        return RefreshErrorCode.unknown

    # ------------------------------------------------------------------ event translation

    @staticmethod
    def _event_to_message_content(event: HarnessEvent) -> MessageContent | None:
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
    def _event_to_ws_message(session_id: str, event: HarnessEvent) -> dict[str, Any] | None:
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

    async def create_pending_question(
        self,
        session_id: str,
        questions: list[dict[str, Any]],
    ) -> str | None:
        """Called by the ask MCP server (via REST) when the model invokes
        `mcp__ask__user`. Generates a question_id, records the pending
        question, broadcasts the `question_request` WS event so the
        frontend renders the form, and schedules the auto-answer
        timeout. Returns the question_id (which the MCP server then
        passes to the long-poll endpoint).
        """
        session = self.sessions.get(session_id)
        if session is None:
            return None
        question_id = uuid.uuid4().hex[:16]
        session._pending_questions[question_id] = PendingQuestion(
            question_id=question_id,
            questions=questions,
        )
        session._pending_question_events[question_id] = asyncio.Event()

        # Persist + broadcast a `question_request` matching the shape the
        # frontend already expects. The persisted MessageContent makes
        # the question visible in chat history on reconnect.
        msg = MessageContent(
            role=MessageRole.assistant,
            type="question_request",
            tool_name="AskUserQuestion",
            tool_use_id=question_id,
            tool_input={"questions": questions},
        )
        msg_seq = await self._persist_message(session, msg)
        event: dict[str, Any] = {
            "type": "question_request",
            "session_id": session.id,
            "question_id": question_id,
            "questions": questions,
        }
        if msg_seq is not None:
            event["seq"] = msg_seq
        await self._broadcast(event)
        self._schedule_question_timeout(session, question_id)
        return question_id

    async def wait_for_question_answer(
        self,
        session_id: str,
        question_id: str,
        timeout: float = 60.0,
    ) -> str | None:
        """Long-poll waiter used by the ask MCP server's HTTP loop.

        Returns the answer text when the user (or auto-answer) submits,
        None on timeout. The MCP server retries on None until it gets
        an answer or hits its own outer limit.
        """
        session = self.sessions.get(session_id)
        if session is None:
            return None
        ev = session._pending_question_events.get(question_id)
        if ev is None:
            # Already-delivered case: answer might be sitting in the
            # answers dict from a fast delivery; return immediately.
            ans = session._pending_question_answers.get(question_id)
            return ans
        try:
            await asyncio.wait_for(ev.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return None
        return session._pending_question_answers.get(question_id)

    async def answer_question(
        self,
        session_id: str,
        question_id: str,
        answers: list[dict[str, Any]],
    ) -> bool:
        """Called by the frontend (REST or legacy WS) when the user
        submits answers to the form. Formats them, stores the text,
        wakes any waiting MCP-server long-poll via the asyncio.Event,
        persists the chat history entry, and broadcasts the WS event.
        """
        session = self.sessions.get(session_id)
        if not session:
            return False
        pending = session._pending_questions.get(question_id)
        if pending is None:
            return False
        answer_text = self._format_answers(pending.questions, answers)
        return await self._deliver_question_answer(
            session, question_id, answer_text, auto=False
        )

    async def _deliver_question_answer(
        self,
        session: Session,
        question_id: str,
        answer_text: str,
        *,
        auto: bool,
    ) -> bool:
        """Common path for both human and timeout-driven answers.

        Sets the per-question Event so the ask MCP server's long-poll
        unblocks and returns the answer to the model. Persists the
        user-visible question_answer chat entry, broadcasts the WS
        event with the `auto` flag set when the timeout fired.
        """
        self._cancel_question_timer(session, question_id)

        # Stash the text + signal the waiter. Even if no MCP long-poll
        # is currently waiting (e.g. the MCP request retried just now),
        # the answer sits in the answers dict for the next poll.
        session._pending_question_answers[question_id] = answer_text
        ev = session._pending_question_events.get(question_id)
        if ev is not None:
            ev.set()

        ans_msg = MessageContent(
            role=MessageRole.user,
            type="question_answer",
            tool_use_id=question_id,
            content=answer_text,
        )
        ans_seq = await self._persist_message(session, ans_msg)
        event: dict[str, Any] = {
            "type": "question_answer",
            "session_id": session.id,
            "question_id": question_id,
            "content": answer_text,
        }
        if auto:
            event["auto"] = True
        if ans_seq is not None:
            event["seq"] = ans_seq
        await self._broadcast(event)

        # Keep the answer text around briefly for any in-flight MCP
        # long-poll that arrives just AFTER set() — it'll fetch from
        # the answers dict directly. We clean up at session reset /
        # delete / archive instead of immediately, since a stale
        # answer dict entry is cheap.
        session._pending_questions.pop(question_id, None)
        return True

    # ---- AskUserQuestion auto-answer on timeout ------------------------------

    AUTO_ANSWER_TEXT = (
        "No human is available to answer this question right now. "
        "Proceed with the task autonomously and try hard to finish it without "
        "asking again. Make the most reasonable choice and continue.\n\n"
        "Only stop and leave a clear note describing what you would have done "
        "if the next action is genuinely risky or irreversible — for example: "
        "destroying data, force-pushing or rewriting shared git history, "
        "deploying to production, modifying billing/payments, sending "
        "messages or emails to external recipients, or running commands that "
        "affect shared infrastructure. For everything else (ambiguous design "
        "choices, formatting, library picks, small refactors), pick the most "
        "reasonable option and keep going."
    )

    def _schedule_question_timeout(self, session: Session, question_id: str) -> None:
        timeout = settings.ask_user_question_timeout_seconds
        if timeout <= 0:
            return  # auto-answer disabled
        # Replace any existing timer for this question_id — defensive,
        # we don't expect the same id to be emitted twice.
        self._cancel_question_timer(session, question_id)
        task = asyncio.create_task(
            self._auto_answer_after(session, question_id, timeout),
            name=f"auto-answer-{session.id}-{question_id}",
        )
        session._question_timers[question_id] = task

    def _cancel_question_timer(self, session: Session, question_id: str) -> None:
        task = session._question_timers.pop(question_id, None)
        if task and not task.done():
            task.cancel()

    def _cancel_all_question_timers(self, session: Session) -> None:
        for task in list(session._question_timers.values()):
            if not task.done():
                task.cancel()
        session._question_timers.clear()

    async def _auto_answer_after(
        self, session: Session, question_id: str, timeout: float
    ) -> None:
        try:
            await asyncio.sleep(timeout)
        except asyncio.CancelledError:
            return  # the user (or a cleanup path) cancelled us first
        # The user might have answered during the sleep — re-check.
        if question_id not in session._pending_questions:
            return
        # Pop our own timer entry so _deliver_question_answer doesn't
        # try to cancel a task that's currently running (self).
        session._question_timers.pop(question_id, None)
        try:
            await self._deliver_question_answer(
                session, question_id, self.AUTO_ANSWER_TEXT, auto=True
            )
        except Exception:
            logger.exception(
                "Auto-answer for session %s question %s failed",
                session.id,
                question_id,
            )

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


def _guess_mime(filename: str) -> str:
    """Lightweight MIME guess for replayed attachments.

    Mirrors the upload-time logic in `server.attachments._detect_mime`,
    but we don't have the client's declared MIME at replay so we always
    derive from the filename extension.
    """
    import mimetypes

    guess, _ = mimetypes.guess_type(filename)
    return guess or "application/octet-stream"


def _split_tool_list(raw: str | None) -> list[str] | None:
    """Parse an agent's newline-separated tool/MCP name list.

    Empty / whitespace-only → None (meaning "no restriction" for allow,
    "nothing extra" for deny). Order preserved, blanks dropped.
    """
    if not raw:
        return None
    items = [line.strip() for line in raw.splitlines() if line.strip()]
    return items or None


def _augment_prompt_with_attachments(prompt: str, paths: list[str]) -> str:
    """Prepend an `<attachments>` block listing absolute paths.

    The agent (Claude Code, Codex, anything with a Read tool) sees the
    paths in its input and can open them on demand. Format kept terse
    and obvious — one path per line so the model doesn't have to parse
    anything clever.
    """
    if not paths:
        return prompt
    lines = ["<attachments>"]
    lines.extend(f"- {p}" for p in paths)
    lines.append("</attachments>")
    lines.append("")
    lines.append(prompt)
    return "\n".join(lines)


# Singleton
session_manager = SessionManager()
