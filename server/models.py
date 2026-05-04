from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class SessionStatus(str, Enum):
    idle = "idle"
    running = "running"
    waiting_approval = "waiting_approval"


class CreateSessionRequest(BaseModel):
    name: str = "New Session"
    working_dir: str | None = None


class ImportSessionRequest(BaseModel):
    name: str = "Imported Session"
    working_dir: str | None = None
    claude_session_id: str | None = None
    messages: list[MessageContent] = []


class SessionInfo(BaseModel):
    id: str
    name: str
    working_dir: str
    status: SessionStatus
    created_at: str
    message_count: int = 0
    claude_session_id: str | None = None


class MessageRole(str, Enum):
    user = "user"
    assistant = "assistant"
    system = "system"
    tool = "tool"


class MessageContent(BaseModel):
    role: MessageRole
    type: str  # "text", "tool_use", "tool_result", "error", "result"
    content: Any = None
    tool_name: str | None = None
    tool_input: dict[str, Any] | None = None
    tool_use_id: str | None = None
    is_error: bool | None = None
    session_id: str | None = None
    cost: float | None = None


class SessionDetail(SessionInfo):
    messages: list[MessageContent] = []


# WebSocket protocol messages (client -> server)

class WsSendMessage(BaseModel):
    type: str = "send_message"
    session_id: str
    content: str


class WsToolDecision(BaseModel):
    type: str  # "approve_tool" or "deny_tool"
    session_id: str
    tool_use_id: str
    reason: str | None = None


# Schedules

class ScheduleInfo(BaseModel):
    id: str
    session_id: str
    name: str
    prompt: str
    interval_seconds: int
    enabled: bool
    created_at: str
    last_run_at: str | None = None


class CreateScheduleRequest(BaseModel):
    session_id: str
    name: str
    prompt: str
    interval_seconds: int = Field(ge=60)


class UpdateScheduleRequest(BaseModel):
    name: str | None = None
    prompt: str | None = None
    interval_seconds: int | None = Field(default=None, ge=60)
    enabled: bool | None = None
