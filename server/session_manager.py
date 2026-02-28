from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncIterator

from claude_code_sdk import (
    AssistantMessage,
    ClaudeCodeOptions,
    ClaudeSDKClient,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolUseBlock,
    ToolResultBlock,
    UserMessage,
)
from claude_code_sdk._errors import MessageParseError

from .config import settings
from .database import Database
from .models import MessageContent, MessageRole, SessionStatus

logger = logging.getLogger(__name__)


@dataclass
class PendingApproval:
    tool_name: str
    tool_input: dict[str, Any]
    tool_use_id: str
    future: asyncio.Future


@dataclass
class Session:
    id: str
    name: str
    working_dir: str
    status: SessionStatus = SessionStatus.idle
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    messages: list[MessageContent] = field(default_factory=list)
    claude_session_id: str | None = None
    _client: ClaudeSDKClient | None = field(default=None, repr=False)
    _pending_approvals: dict[str, PendingApproval] = field(default_factory=dict, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)


class SessionManager:
    def __init__(self) -> None:
        self.sessions: dict[str, Session] = {}
        self._broadcast_callbacks: list[Any] = []
        self.db: Database | None = None

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
            )
            # Load persisted messages
            msg_rows = await db.load_messages(session.id)
            for m in msg_rows:
                session.messages.append(MessageContent(**m))
            self.sessions[session.id] = session
        logger.info("Loaded %d sessions from database", len(rows))

    def on_broadcast(self, callback):
        self._broadcast_callbacks.append(callback)

    def remove_broadcast(self, callback):
        self._broadcast_callbacks = [c for c in self._broadcast_callbacks if c is not callback]

    async def _broadcast(self, message: dict):
        for cb in self._broadcast_callbacks:
            try:
                await cb(message)
            except Exception:
                logger.exception("Broadcast callback error")

    def list_sessions(self) -> list[Session]:
        return list(self.sessions.values())

    def get_session(self, session_id: str) -> Session | None:
        return self.sessions.get(session_id)

    async def create_session(self, name: str, working_dir: str | None = None) -> Session:
        sid = uuid.uuid4().hex[:12]
        session = Session(
            id=sid,
            name=name,
            working_dir=working_dir or settings.default_working_dir,
        )
        self.sessions[sid] = session
        if self.db:
            await self.db.save_session(
                session_id=session.id,
                name=session.name,
                working_dir=session.working_dir,
                created_at=session.created_at,
                claude_session_id=session.claude_session_id,
            )
        return session

    async def import_session(
        self,
        name: str,
        working_dir: str | None = None,
        claude_session_id: str | None = None,
        messages: list[MessageContent] | None = None,
    ) -> Session:
        sid = uuid.uuid4().hex[:12]
        session = Session(
            id=sid,
            name=name,
            working_dir=working_dir or settings.default_working_dir,
            claude_session_id=claude_session_id,
        )
        self.sessions[sid] = session
        if self.db:
            await self.db.save_session(
                session_id=session.id,
                name=session.name,
                working_dir=session.working_dir,
                created_at=session.created_at,
                claude_session_id=session.claude_session_id,
            )
        if messages:
            for msg in messages:
                session.messages.append(msg)
                await self._persist_message(session, msg)
        return session

    async def delete_session(self, session_id: str) -> bool:
        session = self.sessions.pop(session_id, None)
        if session is None:
            return False
        if session._client:
            try:
                await session._client.disconnect()
            except Exception:
                pass
        if self.db:
            await self.db.delete_session(session_id)
        return True

    async def _persist_message(self, session: Session, msg: MessageContent) -> None:
        if not self.db:
            return
        seq = len(session.messages) - 1
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

    async def send_message(
        self, session_id: str, prompt: str
    ) -> AsyncIterator[dict[str, Any]]:
        session = self.sessions.get(session_id)
        if session is None:
            raise ValueError(f"Session {session_id} not found")

        async with session._lock:
            # Record user message
            user_msg = MessageContent(
                role=MessageRole.user, type="text", content=prompt
            )
            session.messages.append(user_msg)
            await self._persist_message(session, user_msg)
            yield {"type": "user_message", "session_id": session_id, "content": prompt}

            session.status = SessionStatus.running
            await self._broadcast(
                {"type": "status", "session_id": session_id, "status": "running"}
            )

            try:
                async for event in self._run_claude(session, prompt):
                    yield event
            except Exception as e:
                logger.exception("Claude error in session %s", session_id)
                error_msg = MessageContent(
                    role=MessageRole.system,
                    type="error",
                    content=str(e),
                )
                session.messages.append(error_msg)
                await self._persist_message(session, error_msg)
                yield {
                    "type": "error",
                    "session_id": session_id,
                    "message": str(e),
                }
            finally:
                session.status = SessionStatus.idle
                await self._broadcast(
                    {"type": "status", "session_id": session_id, "status": "idle"}
                )

    async def _make_permission_handler(self, session: Session):
        async def handler(tool_name, input_data, context):
            tool_use_id = uuid.uuid4().hex[:16]
            loop = asyncio.get_running_loop()
            future = loop.create_future()
            pending = PendingApproval(
                tool_name=tool_name,
                tool_input=input_data,
                tool_use_id=tool_use_id,
                future=future,
            )
            session._pending_approvals[tool_use_id] = pending
            session.status = SessionStatus.waiting_approval

            # Notify frontend about pending approval
            approval_msg = {
                "type": "tool_approval_request",
                "session_id": session.id,
                "tool_use_id": tool_use_id,
                "tool_name": tool_name,
                "tool_input": input_data,
            }
            await self._broadcast(approval_msg)
            msg = MessageContent(
                role=MessageRole.tool,
                type="tool_approval_request",
                tool_name=tool_name,
                tool_input=input_data,
                tool_use_id=tool_use_id,
            )
            session.messages.append(msg)
            await self._persist_message(session, msg)

            # Wait for user decision
            try:
                result = await future
            finally:
                session._pending_approvals.pop(tool_use_id, None)
                if not session._pending_approvals:
                    session.status = SessionStatus.running

            return result

        return handler

    @staticmethod
    async def _receive_safe(client: ClaudeSDKClient):
        """Iterate over SDK responses, skipping messages the SDK can't parse."""
        it = client.receive_response().__aiter__()
        while True:
            try:
                msg = await it.__anext__()
            except StopAsyncIteration:
                logger.info("SDK stream ended (StopAsyncIteration)")
                break
            except MessageParseError as e:
                logger.warning("Skipping unparseable SDK message: %s", e)
                continue
            except Exception as e:
                logger.error("SDK stream error (continuing): %s: %s", type(e).__name__, e)
                continue
            logger.info("SDK message: %s", type(msg).__name__)
            yield msg

    async def _run_claude(
        self, session: Session, prompt: str
    ) -> AsyncIterator[dict[str, Any]]:
        # Build options
        opts_kwargs: dict[str, Any] = {
            "cwd": session.working_dir,
            "permission_mode": "bypassPermissions",
        }

        if session.claude_session_id:
            opts_kwargs["resume"] = session.claude_session_id

        options = ClaudeCodeOptions(**opts_kwargs)

        async with ClaudeSDKClient(options) as client:
            session._client = client
            try:
                await client.query(prompt)

                async for msg in self._receive_safe(client):
                    if isinstance(msg, AssistantMessage):
                        for block in msg.content:
                            if isinstance(block, TextBlock):
                                text_msg = MessageContent(
                                    role=MessageRole.assistant,
                                    type="text",
                                    content=block.text,
                                )
                                session.messages.append(text_msg)
                                await self._persist_message(session, text_msg)
                                yield {
                                    "type": "assistant_text",
                                    "session_id": session.id,
                                    "content": block.text,
                                }
                            elif isinstance(block, ToolUseBlock):
                                tool_msg = MessageContent(
                                    role=MessageRole.assistant,
                                    type="tool_use",
                                    tool_name=block.name,
                                    tool_input=block.input,
                                    tool_use_id=block.id,
                                )
                                session.messages.append(tool_msg)
                                await self._persist_message(session, tool_msg)
                                yield {
                                    "type": "tool_use",
                                    "session_id": session.id,
                                    "tool": block.name,
                                    "input": block.input,
                                    "tool_use_id": block.id,
                                }
                            elif isinstance(block, ToolResultBlock):
                                result_content = block.content
                                if isinstance(result_content, list):
                                    result_content = str(result_content)
                                result_msg = MessageContent(
                                    role=MessageRole.tool,
                                    type="tool_result",
                                    content=result_content,
                                    tool_use_id=block.tool_use_id,
                                    is_error=block.is_error,
                                )
                                session.messages.append(result_msg)
                                await self._persist_message(session, result_msg)
                                yield {
                                    "type": "tool_result",
                                    "session_id": session.id,
                                    "tool_use_id": block.tool_use_id,
                                    "output": result_content,
                                    "is_error": block.is_error,
                                }

                    elif isinstance(msg, UserMessage):
                        # Tool results echoed back
                        pass

                    elif isinstance(msg, ResultMessage):
                        session.claude_session_id = msg.session_id
                        if self.db:
                            await self.db.update_session_field(
                                session.id, claude_session_id=msg.session_id
                            )
                        result_msg = MessageContent(
                            role=MessageRole.system,
                            type="result",
                            session_id=msg.session_id,
                            cost=msg.total_cost_usd,
                        )
                        session.messages.append(result_msg)
                        await self._persist_message(session, result_msg)
                        yield {
                            "type": "result",
                            "session_id": session.id,
                            "claude_session_id": msg.session_id,
                            "cost": msg.total_cost_usd,
                            "turns": msg.num_turns,
                            "duration_ms": msg.duration_ms,
                            "is_error": msg.is_error,
                        }

                    elif isinstance(msg, SystemMessage):
                        pass
            finally:
                session._client = None

    async def approve_tool(self, session_id: str, tool_use_id: str) -> bool:
        session = self.sessions.get(session_id)
        if not session:
            return False
        pending = session._pending_approvals.get(tool_use_id)
        if not pending or pending.future.done():
            return False
        pending.future.set_result(PermissionResultAllow())
        return True

    async def deny_tool(
        self, session_id: str, tool_use_id: str, reason: str = ""
    ) -> bool:
        session = self.sessions.get(session_id)
        if not session:
            return False
        pending = session._pending_approvals.get(tool_use_id)
        if not pending or pending.future.done():
            return False
        pending.future.set_result(
            PermissionResultDeny(message=reason or "Denied by user")
        )
        return True


# Singleton
session_manager = SessionManager()
