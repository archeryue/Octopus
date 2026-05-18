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
    credential_id: str | None = None


class ImportSessionRequest(BaseModel):
    name: str = "Imported Session"
    working_dir: str | None = None
    claude_session_id: str | None = None
    credential_id: str | None = None
    messages: list[MessageContent] = []


class SessionInfo(BaseModel):
    id: str
    name: str
    working_dir: str
    status: SessionStatus
    created_at: str
    message_count: int = 0
    claude_session_id: str | None = None
    credential_id: str | None = None
    # Hidden from the default `GET /api/sessions` list; surfaced only
    # when the caller passes `?include_archived=true` (or for individual
    # GETs by id, which always work). The `/archive` flow sets this;
    # `/unarchive` clears it.
    archived: bool = False


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
    # Per-session monotonic sequence. Set when the message is loaded from
    # DB or persisted; clients use it to dedupe WebSocket events against
    # the snapshot returned by `GET /api/sessions/{id}` after a reconnect.
    seq: int | None = None


class PendingQuestionInfo(BaseModel):
    question_id: str
    questions: list[dict[str, Any]]


class SessionDetail(SessionInfo):
    messages: list[MessageContent] = []
    pending_queue: list[str] = []
    pending_questions: list[PendingQuestionInfo] = []
    # High-water mark of the messages above: the seq of the next message
    # the server will assign. Frontends use this to set their dedup
    # baseline so any subsequently-broadcast event with seq <=
    # next_message_seq-1 is treated as already applied.
    next_message_seq: int = 0


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


# Backend credentials


class BackendKind(str, Enum):
    claude_code = "claude-code"
    codex = "codex"


class AuthType(str, Enum):
    api_key = "api_key"
    oauth = "oauth"


class CredentialStatus(str, Enum):
    active = "active"
    needs_reconnect = "needs_reconnect"


class CredentialInfo(BaseModel):
    """Credential metadata returned to clients — never includes the secret.

    Refresh-state fields (Steal Plan B-4 / B-5) are populated for OAuth
    providers that issue short-lived access tokens. Claude Code's
    long-lived `sk-ant-` key leaves them null today; they're here so the
    UI can render "needs reconnect" once a refresh-token provider lands
    without another schema/contract pump.
    """

    id: str
    backend: BackendKind
    label: str
    auth_type: AuthType
    created_at: str
    status: CredentialStatus = CredentialStatus.active
    token_expires_at: str | None = None
    needs_reconnect: bool = False
    last_refresh_error_code: str | None = None


class CreateCredentialRequest(BaseModel):
    backend: BackendKind
    label: str
    auth_type: AuthType = AuthType.api_key
    secret: str = Field(min_length=1)


class UpdateCredentialRequest(BaseModel):
    label: str | None = None
    secret: str | None = Field(default=None, min_length=1)


# Notifiers


class NotifierType(str, Enum):
    webhook = "webhook"


class NotifierInfo(BaseModel):
    """Notifier metadata returned to clients.

    `config` is type-specific (e.g. webhook: `{"url": "..."}`). Clients
    treat it as opaque except for the keys they know how to render.
    """

    id: str
    type: NotifierType
    label: str
    config: dict[str, Any] = {}
    enabled: bool = True
    created_at: str


class CreateNotifierRequest(BaseModel):
    type: NotifierType
    label: str = Field(min_length=1)
    config: dict[str, Any] = {}


class UpdateNotifierRequest(BaseModel):
    label: str | None = None
    config: dict[str, Any] | None = None
    enabled: bool | None = None
