"""Converting a HarnessEvent into what gets persisted and what gets broadcast.

One slice of `SessionManager`, which was a single 4,017-line class with 84
methods (docs/plans/polish-2026-09.md §3 A1). Split by responsibility; the
methods are unchanged and still reached through one `session_manager` object,
so no call site moved.
"""

from __future__ import annotations

from typing import Any

from ..harness import HarnessEvent
from ..models import MessageContent, MessageRole
from .base import (
    _MAX_SUBAGENTS_PER_SESSION,
    Session,
    SessionManagerBase,
)


class EventsMixin(SessionManagerBase):

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
    def _record_subagent(session: Session, update: Any) -> None:
        """Merge one sub-agent observation into the session's live map.

        Every harness reports sub-agents in partial shapes — a status patch
        with no name, a summary with no counters — so each observation is
        merged onto the last (`SubagentUpdate.merged_with`). Bounded, oldest
        first: a long session can spawn many, and this is UI state, not
        history.
        """
        key = update.tool_use_id or update.task_id
        if not key:
            return
        session._subagents[key] = update.merged_with(session._subagents.get(key))
        while len(session._subagents) > _MAX_SUBAGENTS_PER_SESSION:
            session._subagents.pop(next(iter(session._subagents)))

    def _flush_text_deltas(
        self, session_id: str, buf: list[str]
    ) -> dict[str, Any] | None:
        """Drain the coalescing buffer into one `assistant_delta` frame.

        Empties `buf` in place and returns None when there was nothing to send,
        so callers can flush unconditionally. The frame shape comes from
        `_event_to_ws_message` rather than being rebuilt here — one definition
        of the wire format, not two.
        """
        if not buf:
            return None
        text = "".join(buf)
        buf.clear()
        return self._event_to_ws_message(
            session_id, HarnessEvent(type="text_delta", content=text)
        )

    @staticmethod
    def _event_to_ws_message(session_id: str, event: HarnessEvent) -> dict[str, Any] | None:
        if event.type == "text_delta":
            # Broadcast-only (never persisted — `_event_to_message_content`
            # has no branch for it). The client appends these to a scratch
            # buffer and drops the buffer when the real `assistant_text`
            # lands, so losing one costs a flicker, not a message.
            if not event.content:
                return None
            return {
                "type": "assistant_delta",
                "session_id": session_id,
                "content": event.content,
            }
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
        if event.type == "subagent" and event.subagent is not None:
            u = event.subagent
            return {
                "type": "subagent",
                "session_id": session_id,
                "task_id": u.task_id,
                "tool_use_id": u.tool_use_id,
                "status": u.status,
                "name": u.name,
                "description": u.description,
                "prompt": u.prompt,
                "summary": u.summary,
                "tokens": u.tokens,
                "tool_uses": u.tool_uses,
                "duration_ms": u.duration_ms,
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
