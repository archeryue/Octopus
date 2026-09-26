"""A session's life: create, archive, unarchive, import, delete, evict.

One slice of `SessionManager`, which was a single 4,017-line class with 84
methods (docs/plans/polish-2026-09.md §3 A1). Split by responsibility; the
methods are unchanged and still reached through one `session_manager` object,
so no call site moved.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from ..aio import stopped_within
from ..attachments import delete_session_attachments
from ..large_prompts import delete_session_large_prompts
from ..models import MessageContent, SessionDetail, SessionStatus
from .base import (
    Session,
    SessionManagerBase,
    _session_fork_kwargs,
    fork_info_fields,
    resolve_working_dir,
)

# The window an archived open returns, matching the live one in
# `routers/sessions.MESSAGE_WINDOW`. Declared here rather than imported to keep
# the slice free of a router import; the read-only manage view pages back
# through the same `…/messages` cursor.
_ARCHIVED_MESSAGE_WINDOW = 200


class LifecycleMixin(SessionManagerBase):

    async def create_session(
        self,
        agent_id: str | None,
        name: str | None = None,
        working_dir: str | None = None,
        credential_id: str | None = None,
        origin: str = "user",
        backend: str = "claude-code",
        parent_session_id: str | None = None,
        delegation_request: str | None = None,
        app_id: str | None = None,
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
            stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M")
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
            app_id=app_id,
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
                app_id=session.app_id,
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

        Schedules are owned by the *Agent* now, not the session, so there
        is nothing to repoint (agent-refactor.md §5.2).

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
            await stopped_within(
                old._backend.stop(), f"session {old.id} backend", timeout=2.0
            )
            self._forget_backend(old)
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

        # Schedules are agent-owned, so ownership needs no repoint.
        # But a schedule created from this session (origin_session_id == old.id)
        # should follow the live successor thread, otherwise its runs fall back
        # to throwaway sessions. Move them onto `new`, then re-register the live
        # jobs so the next fire targets the successor, not the archived
        # session.
        if self.db:
            repointed = await self.db.repoint_schedules_origin(old.id, new.id)
            if self._schedule_runner is not None:
                for row in repointed:
                    await self._schedule_runner.reschedule(row)

        await self._broadcast(
            {
                "type": "session_archived",
                "old_session_id": old.id,
                "new_session_id": new.id,
                "name": new.name,
            }
        )
        return new

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
                await stopped_within(
                    session._backend.stop(),
                    f"session {session.id} backend",
                    timeout=2.0,
                )
                self._forget_backend(session)
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
                    **fork_info_fields(
                        backend=row.get("backend") or "claude-code",
                        forked_from_session_id=row.get("forked_from_session_id"),
                        fork_after_seq=row.get("fork_after_seq"),
                        fork_metadata=row.get("fork_metadata"),
                        fork_revert_record=row.get("fork_revert_record"),
                    ),
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
        # Windowed like a live open, and counted separately: `len(messages)`
        # stopped being the message count once a window is all we fetch
        # (polish-2026-09.md §4 B2).
        messages_raw = await self.db.load_messages(
            session_id, limit=_ARCHIVED_MESSAGE_WINDOW, newest_first=True
        )
        messages = [MessageContent(**m) for m in messages_raw]
        total = await self.db.count_messages(session_id)
        oldest = messages[0].seq if messages else None
        return SessionDetail(
            id=match["id"],
            name=match["name"],
            working_dir=match["working_dir"],
            status=SessionStatus.idle,
            created_at=match["created_at"],
            message_count=total,
            claude_session_id=match["claude_session_id"],
            credential_id=match.get("credential_id"),
            agent_id=match.get("agent_id"),
            origin=match.get("origin") or "user",
            backend=match.get("backend") or "claude-code",
            parent_session_id=match.get("parent_session_id"),
            delegation_request=match.get("delegation_request"),
            **fork_info_fields(
                backend=match.get("backend") or "claude-code",
                forked_from_session_id=match.get("forked_from_session_id"),
                fork_after_seq=match.get("fork_after_seq"),
                fork_metadata=match.get("fork_metadata"),
                fork_revert_record=match.get("fork_revert_record"),
            ),
            archived=True,
            messages=messages,
            pending_queue=[],
            pending_questions=[],
            oldest_loaded_seq=oldest,
            has_more_messages=bool(oldest),
            next_message_seq=total,
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
            **_session_fork_kwargs(match),
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
            await stopped_within(
                session._backend.stop(), f"session {session.id} backend"
            )
        # Blit attachment files into descendant forks BEFORE removing this
        # session's dir, so the read-time fallback stays valid (§5.5). Done
        # before the DB delete so the descendant message rows are still
        # readable for their attachment references.
        await self._blit_attachments_to_descendant_forks(session_id)
        if self.db:
            await self.db.delete_session(session_id)
        # Best-effort: wipe any uploaded files for this session. We do
        # this after the DB delete so the FK cascade has already removed
        # message rows pointing at them — if rmtree fails, the session
        # row is still gone, which is the user-visible expectation.
        delete_session_attachments(session_id)
        delete_session_large_prompts(session_id)
        return True
