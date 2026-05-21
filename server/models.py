from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class SessionStatus(str, Enum):
    idle = "idle"
    running = "running"
    waiting_approval = "waiting_approval"


class BackendKind(str, Enum):
    claude_code = "claude-code"
    codex = "codex"


class CreateSessionRequest(BaseModel):
    name: str | None = None
    working_dir: str | None = None
    credential_id: str | None = None
    # Owning agent. Required by the API (a session is a conversation with an
    # agent), but left optional on the wire for exactly one release: when
    # omitted the route falls back to the Default Agent. See
    # docs/plans/agent-refactor.md §5.4.
    agent_id: str | None = None
    # Which AI backend drives this session (codex-backend.md §4.1).
    backend: BackendKind = BackendKind.claude_code


class ImportSessionRequest(BaseModel):
    name: str = "Imported Session"
    working_dir: str | None = None
    claude_session_id: str | None = None
    credential_id: str | None = None
    agent_id: str | None = None  # owner; Default Agent when omitted
    backend: BackendKind = BackendKind.claude_code
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
    # Owning agent + who created the session ('user' | 'schedule' | 'bridge').
    agent_id: str | None = None
    origin: str = "user"
    # Which AI backend drives this session.
    backend: BackendKind = BackendKind.claude_code
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


class AttachmentMetadata(BaseModel):
    """Metadata for a user-uploaded file attached to a message.

    Only what clients need to render the chip / fetch the file. The
    on-disk path lives in `server.attachments` and is derivable from
    `session_id + id` — we don't ship it to the client.
    """

    id: str
    filename: str
    size: int
    mime_type: str


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
    # User-uploaded attachments associated with this message (only ever
    # set on user-role messages today). Persisted as JSON in the DB and
    # round-trips back via load_messages.
    attachments: list[AttachmentMetadata] = []
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
    # IDs of attachments previously uploaded via
    # `POST /api/sessions/{id}/attachments`. The session manager resolves
    # them to absolute disk paths and prepends a `<attachments>` block to
    # the prompt so the agent can `Read` them.
    attachment_ids: list[str] = []


class WsToolDecision(BaseModel):
    type: str  # "approve_tool" or "deny_tool"
    session_id: str
    tool_use_id: str
    reason: str | None = None


# Schedules

class ScheduleInfo(BaseModel):
    id: str
    agent_id: str
    name: str
    prompt: str
    interval_seconds: int
    enabled: bool
    created_at: str
    last_run_at: str | None = None


class CreateScheduleRequest(BaseModel):
    name: str
    prompt: str
    interval_seconds: int = Field(ge=60)
    # Agent-scoped routes (`/api/agents/{id}/schedules`) take the agent from
    # the path; these are for the standalone `/api/schedules` route. Provide
    # exactly one — `agent_id` directly, or `session_id` (legacy compat,
    # resolved to the session's agent for one release).
    agent_id: str | None = None
    session_id: str | None = None


class UpdateScheduleRequest(BaseModel):
    name: str | None = None
    prompt: str | None = None
    interval_seconds: int | None = Field(default=None, ge=60)
    enabled: bool | None = None


# Agents — the durable definition of an assistant (agent-refactor.md §4).


class AgentRead(BaseModel):
    id: str
    name: str
    description: str = ""
    avatar: str | None = None
    system_prompt: str = ""
    model: str | None = None
    credential_id: str | None = None
    mcp_servers: list[str] = []
    # Newline-separated tool/MCP name lists. Empty `tool_allow` = allow all;
    # `tool_deny` wins on conflict.
    tool_allow: str = ""
    tool_deny: str = ""
    is_system: bool = False
    archived: bool = False
    created_at: str
    updated_at: str
    active_session_count: int = 0


class AgentCreate(BaseModel):
    name: str = Field(min_length=1)
    description: str = ""
    avatar: str | None = None
    system_prompt: str = ""
    model: str | None = None
    credential_id: str | None = None
    mcp_servers: list[str] = ["ask", "bg", "viewer"]
    tool_allow: str = ""
    tool_deny: str = ""


class AgentUpdate(BaseModel):
    # All optional; the route applies only the fields explicitly provided
    # (model_dump(exclude_unset=True)), so passing null clears a nullable
    # field while omitting it leaves the field untouched.
    name: str | None = None
    description: str | None = None
    avatar: str | None = None
    system_prompt: str | None = None
    model: str | None = None
    credential_id: str | None = None
    mcp_servers: list[str] | None = None
    tool_allow: str | None = None
    tool_deny: str | None = None


# Backend credentials


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


# Connectors (connectors.md). Installations are global; enablement is
# agent-scoped (the agent_connectors join). Secrets are never returned.


class ConnectorCatalogEntry(BaseModel):
    kind: str
    display_name: str
    category: str
    allows_multiple: bool
    available: bool  # both OAuth client id + secret configured in env


class ConnectorInstallationInfo(BaseModel):
    id: str
    kind: str
    label: str
    auth_type: AuthType = AuthType.oauth
    external_account_id: str | None = None
    scopes: list[str] = []
    enable_by_default: bool = False
    needs_reconnect: bool = False
    token_expires_at: str | None = None
    last_refresh_error_code: str | None = None
    created_at: str


class ConnectorOAuthStartRequest(BaseModel):
    kind: str
    label: str | None = None


class ConnectorOAuthStartResponse(BaseModel):
    login_id: str
    authorize_url: str


class ConnectorOAuthStatusResponse(BaseModel):
    status: str  # ConnectorLoginState value
    installation_id: str | None = None
    message: str | None = None


class ConnectorOAuthCancelRequest(BaseModel):
    login_id: str


class UpdateConnectorRequest(BaseModel):
    label: str | None = None
    enable_by_default: bool | None = None


class ConnectorTokenResponse(BaseModel):
    """Internal — returned only to the connector MCP subprocess."""

    access_token: str
    expires_at_epoch: float


class AgentConnectorsResponse(BaseModel):
    installation_ids: list[str]


class SetAgentConnectorsRequest(BaseModel):
    installation_ids: list[str]


class ToggleAgentConnectorRequest(BaseModel):
    enabled: bool


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
