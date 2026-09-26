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
    # Which AI backend drives this session (codex-backend.md §4.1). None =
    # inherit the owning agent's default backend (resolved in the route).
    backend: BackendKind | None = None


class SessionUpdate(BaseModel):
    """Fields a live session can be repointed at after creation.

    `credential_id` is the one that matters: a session's engine credential was
    previously fixed at creation, so swapping a lapsed or deleted sign-in
    meant abandoning the conversation. Pass null to fall back to the agent's
    credential (or the CLI's own login)."""

    # `...` default distinguishes "not provided" from an explicit null, which
    # is how you clear the binding.
    credential_id: str | None = Field(default=None)
    name: str | None = None


class ForkSessionRequest(BaseModel):
    """Body for `POST /api/sessions/{id}/fork` (session-rewind.md §5.1).
    Rewind to *before* the user message at `rewind_to_msg_seq` and re-spawn."""

    rewind_to_msg_seq: int
    revert_files: bool = False
    label: str | None = None


class DuplicateSessionRequest(BaseModel):
    """Body for `POST /api/sessions/{id}/duplicate` (session-fork.md).
    Fork the whole session onto an independent full copy of its working dir."""

    label: str | None = None


class ImportSessionRequest(BaseModel):
    name: str = "Imported Session"
    working_dir: str | None = None
    claude_session_id: str | None = None
    credential_id: str | None = None
    agent_id: str | None = None  # owner; Default Agent when omitted
    backend: BackendKind = BackendKind.claude_code
    messages: list[MessageContent] = []


class ForkRevertRecord(BaseModel):
    """Durable record of a fork's safe-revert outcome (session-rewind.md
    §5.6.5). Mirrors the `fork_revert_record` JSON column; surfaced on
    SessionInfo so the UI can render "files were restored / revert refused".
    """

    ran: bool = False
    files: list[str] = []
    stash_ref: str | None = None
    # completed | refused | failed | unknown_post_crash
    status: str
    refused_reason: str | None = None
    error: str | None = None


class SessionInfo(BaseModel):
    id: str
    name: str
    working_dir: str
    status: SessionStatus
    created_at: str
    message_count: int = 0
    claude_session_id: str | None = None
    credential_id: str | None = None
    # Owning agent + who created the session ('user' | 'schedule' |
    # 'delegation' | 'fork' | 'application' | 'app').
    agent_id: str | None = None
    origin: str = "user"
    # Which AI backend drives this session.
    backend: BackendKind = BackendKind.claude_code
    # Agent-to-agent: set on delegation sessions to point at the parent
    # session that spawned them; NULL elsewhere. The verbatim original
    # delegation prompt is kept alongside for UI display.
    # (agent-collaboration.md §4.1)
    parent_session_id: str | None = None
    delegation_request: str | None = None
    # Set on an application's build session and on the conversations the
    # running app holds with an agent (origin='app'). The sidebar hides the
    # latter — an app that talks to an agent all day must not bury the
    # user's own sessions. (app-agent-access.md §3/§7)
    app_id: str | None = None
    # Whether a message typed while this session is running can be handed to
    # the turn in flight instead of queueing behind it (inline-steering.md §8).
    # A backend capability, like `can_fork` below: only a harness that takes
    # input on stdin can be steered. The composer reads it to say what the
    # send button will actually do — claiming "Queue message" when the message
    # will steer is worse than not saying anything.
    can_steer: bool = False
    # Session tree-rewind / fork (session-rewind.md §4). Exactly five
    # fork-related fields are exposed; fork_status, fork_needs_replay and the
    # raw fork_metadata blob are server-internal.
    #   - can_fork: backend capability flag (harness profile)
    #   - forked_from_session_id / fork_after_seq: tree linkage
    #   - fork_prefilled_prompt: rewound user message text (from
    #     fork_metadata.prefilled_prompt while non-null) — populates the
    #     fork's chat input on open
    #   - fork_revert_record: durable safe-revert outcome
    #   - fork_is_full_copy: True for a /fork copy-dir duplicate (carries the
    #     whole history onto an independent working-dir copy), False for a
    #     /rewind branch — the UI renders the banner/badge differently
    can_fork: bool = False
    forked_from_session_id: str | None = None
    fork_after_seq: int | None = None
    fork_prefilled_prompt: str | None = None
    fork_revert_record: ForkRevertRecord | None = None
    fork_is_full_copy: bool = False
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


class SubagentRun(BaseModel):
    """A sub-agent the model spawned inside a turn (native-subagents.md).

    Live UI state, not history: the durable record is the `Task` /
    `collab_tool_call` tool call already in `messages`. Carried on the
    snapshot so a browser reload mid-run doesn't blank the card.
    """

    task_id: str
    tool_use_id: str | None = None
    status: str = "running"
    name: str = ""
    description: str = ""
    prompt: str = ""
    summary: str = ""
    tokens: int | None = None
    tool_uses: int | None = None
    duration_ms: int | None = None
    # What it has been doing, newest last (native-subagents.md §5). Carried on
    # the snapshot so a reload mid-run restores the trail, not just the
    # current line.
    steps: list[str] = []


class SessionDetail(SessionInfo):
    messages: list[MessageContent] = []
    pending_queue: list[str] = []
    pending_questions: list[PendingQuestionInfo] = []
    # Sub-agents still on screen for this session (native-subagents.md §4).
    subagents: list[SubagentRun] = []
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
    # Recurrence is exactly one of `interval_seconds` (fire every N seconds) or
    # `cron` + `timezone` (5-field crontab in that tz). `recurrence_label` is
    # the human-readable description the UI shows.
    id: str
    agent_id: str
    name: str
    prompt: str
    interval_seconds: int | None = None
    cron: str | None = None
    timezone: str | None = None
    recurrence_label: str = ""
    enabled: bool
    created_at: str
    last_run_at: str | None = None
    # The session the `/schedule` command was created in. When set and still
    # live, each fire appends into that conversation; otherwise a fresh
    # schedule-origin session is materialized. Null for agent/API-created ones.
    origin_session_id: str | None = None
    # One-time schedule: fire once at this ISO datetime then auto-delete.
    # Null for recurring schedules (interval or cron).
    run_at: str | None = None
    # The session the most recent fire ran in, so "last run" in the UI opens
    # what actually happened. Null before the first fire, and on rows that
    # predate the column.
    last_run_session_id: str | None = None
    # When this schedule fires next, read from APScheduler's live trigger
    # state. Null when disabled (no job registered). The UI shows it in the
    # sidebar summary row and the Schedules page header.
    next_run_at: str | None = None


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


class ScheduleFromTextRequest(BaseModel):
    """Natural-language schedule: `text` is parsed (rigid fast-path, else AI)
    into a recurrence + prompt. `timezone` is the user's IANA tz (browser-
    detected) so "9am" means their local 9am; `now` lets tests pin the clock."""

    text: str = Field(min_length=1)
    timezone: str | None = None
    now: str | None = None
    # The session the command was typed in. Stored on the schedule so each fire
    # appends the run into that same conversation (rather than a throwaway one).
    session_id: str | None = None


class ShowMeResolveRequest(BaseModel):
    # The user's natural reference after `/showme`, e.g. "this file" or
    # "README.md". The backend asks the session's model to resolve it to a
    # concrete file path, then the client opens the viewer.
    text: str = Field(min_length=1)


class ShowMeResolveResponse(BaseModel):
    # Resolved path relative to the session working directory. When null,
    # `message` explains why the model couldn't resolve it cleanly.
    path: str | None = None
    message: str | None = None


class AgentScheduleRequest(BaseModel):
    """A schedule an agent sets for itself (schedule-tool.md §3).

    Recurrence is stated outright — exactly one of `cron` (+`timezone`),
    `interval_seconds`, or `run_at` (ISO datetime, fires once). Bounds are
    checked in `schedule_ai.build_explicit_schedule` rather than here, so the
    caller gets one sentence explaining what to pass instead of a pydantic
    constraint dump.
    """

    prompt: str = Field(min_length=1)
    name: str | None = None
    cron: str | None = None
    interval_seconds: int | None = None
    run_at: str | None = None
    timezone: str | None = None
    # True: fires append into the conversation the agent asked from, so the
    # run shows up where the schedule was set up. False: each fire gets a
    # throwaway session (continuity comes from agent memory).
    in_session: bool = True


class UpdateScheduleRequest(BaseModel):
    """A change to an existing schedule. Recurrence fields are mutually
    exclusive — setting one clears the other two (a schedule has exactly one
    recurrence), and `timezone` alone re-reads the existing cron in the new
    zone."""

    name: str | None = None
    prompt: str | None = None
    interval_seconds: int | None = None
    cron: str | None = None
    run_at: str | None = None
    timezone: str | None = None
    enabled: bool | None = None


# Agents — the durable definition of an assistant (agent-refactor.md §4).


class AgentRead(BaseModel):
    # use_enum_values keeps `backend` as the plain string ("claude-code")
    # both when built from a DB row and when serialized — matches the TEXT
    # column and avoids enum/value juggling at the storage layer.
    model_config = {"use_enum_values": True}

    id: str
    name: str
    description: str = ""
    avatar: str | None = None
    system_prompt: str = ""
    model: str | None = None
    credential_id: str | None = None
    # Default AI backend for this agent's new sessions (codex-backend.md §4.1).
    # Per-session overrides still win; this is the inherited default.
    backend: BackendKind = BackendKind.claude_code
    mcp_servers: list[str] = []
    # Newline-separated tool/MCP name lists. Empty `tool_allow` = allow all;
    # `tool_deny` wins on conflict.
    tool_allow: str = ""
    tool_deny: str = ""
    # Sub-agents this agent brings with it (native-subagents.md §6), rendered
    # as Claude Code's `--agents` JSON. Empty = the CLI's built-ins only.
    subagents: list[SubagentDefinition] = []
    is_system: bool = False
    archived: bool = False
    created_at: str
    updated_at: str
    active_session_count: int = 0


class SubagentDefinition(BaseModel):
    """One sub-agent an Octopus agent can hand to the CLI.

    Mirrors Claude Code's `--agents` JSON: a name the model addresses, a
    description telling it when to reach for this one, the sub-agent's own
    system prompt, and optional tool/model narrowing.
    """

    name: str = Field(min_length=1, max_length=64)
    description: str = ""
    prompt: str = ""
    model: str | None = None
    # Empty = inherit the parent's tools.
    tools: list[str] = []


class AgentCreate(BaseModel):
    model_config = {"use_enum_values": True}

    name: str = Field(min_length=1)
    description: str = ""
    avatar: str | None = None
    system_prompt: str = ""
    model: str | None = None
    credential_id: str | None = None
    backend: BackendKind = BackendKind.claude_code
    # Legacy default for API callers that don't name a set. The UI sends the
    # full built-in list (AgentFormPage's BUILTIN_MCP); a brand-new row that
    # names nothing gets the DB default instead.
    mcp_servers: list[str] = ["ask", "bg"]
    tool_allow: str = ""
    tool_deny: str = ""
    subagents: list[SubagentDefinition] = []


class AgentUpdate(BaseModel):
    # All optional; the route applies only the fields explicitly provided
    # (model_dump(exclude_unset=True)), so passing null clears a nullable
    # field while omitting it leaves the field untouched.
    model_config = {"use_enum_values": True}

    name: str | None = None
    description: str | None = None
    avatar: str | None = None
    system_prompt: str | None = None
    model: str | None = None
    credential_id: str | None = None
    backend: BackendKind | None = None
    mcp_servers: list[str] | None = None
    tool_allow: str | None = None
    tool_deny: str | None = None
    subagents: list[SubagentDefinition] | None = None


class TokenRotateRequest(BaseModel):
    """Change the access token (token-rotation.md)."""

    new_token: str = Field(min_length=1)
    # True when the old token leaked: other signed-in clients are dropped to
    # the login screen instead of being handed the new one.
    revoke_other_clients: bool = False


class TokenRotateResponse(BaseModel):
    # Which env files were rewritten, and how many secrets were re-keyed per
    # table — the UI says this back so a rotation is never a silent success.
    env_files: list[str] = []
    reencrypted: dict[str, int] = {}


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
    available: bool  # OAuth client id + secret configured (in-app or env)
    scopes: list[str] = []  # OAuth scopes requested at sign-in
    custom: bool = False  # user-defined (deletable) vs built-in
    setup_url: str | None = None  # provider's app-registration page
    setup_steps: list[str] = []  # in-app "how to register" guidance


class CustomConnectorCreateRequest(BaseModel):
    """Define a brand-new connector kind from the browser (connectors.md
    custom-connectors). Client creds are stored alongside built-in config."""

    kind: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    authorize_url: str = Field(min_length=1)
    token_url: str = Field(min_length=1)
    scopes: list[str] = []
    pkce: bool = False
    api_base: str = Field(min_length=1)
    client_id: str = Field(min_length=1)
    client_secret: str = Field(min_length=1)


class ConnectorOAuthClientInfo(BaseModel):
    """Non-secret view of a kind's OAuth client config (for the setup dialog)."""

    kind: str
    configured: bool
    client_id: str | None = None
    source: str | None = None  # "db" | "env" | None
    redirect_uri: str


class SetConnectorOAuthClientRequest(BaseModel):
    client_id: str = Field(min_length=1)
    client_secret: str = Field(min_length=1)


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


# Applications (docs/plans/applications.md §2/§5) — agent-built static web
# apps that Octopus serves under /apps/{id}/ and renders in the main pane.


class ApplicationStatus(str, Enum):
    building = "building"
    ready = "ready"
    failed = "failed"


class ApplicationBackend(BaseModel):
    """Runtime state of an application's own server process.

    A second axis alongside `status`, not folded into it: an app can be
    perfectly built with a crashed backend, or mid-rebuild while the old
    backend still serves (application-backends.md §9).
    """

    #: absent | installing | stopped | starting | running | failed
    state: str = "absent"
    port: int | None = None
    error: str | None = None
    uptime_s: float | None = None
    #: Last lines of install/start output — a dead backend with no log is the
    #: failure this feature exists to avoid.
    log_tail: list[str] = Field(default_factory=list)


class ApplicationRead(BaseModel):
    model_config = {"use_enum_values": True}

    id: str
    name: str
    description: str = ""
    icon: str | None = None
    # The app's own icon, discovered from its files after each build: either a
    # path relative to the app dir (served via `/apps/{id}/…`) or a `data:`
    # URI to use verbatim. **Server-owned** — absent from Create and Update, so
    # a rebuild can refresh it and a removed icon can clear it without ever
    # overwriting the emoji a user typed. The UI prefers `icon` when set.
    icon_src: str | None = None
    # Present for every application; `state: "absent"` when it ships no
    # executable start.sh.
    backend: ApplicationBackend = Field(default_factory=ApplicationBackend)
    # The agent that built it and the session its build turns run in. Both
    # nullable: an application outlives the agent and the conversation
    # (ON DELETE SET NULL), it just can't be rebuilt without a new one.
    agent_id: str | None = None
    session_id: str | None = None
    app_dir: str
    entrypoint: str = "index.html"
    status: ApplicationStatus = ApplicationStatus.building
    error: str | None = None
    # Archived applications leave the sidebar but keep their row and files;
    # the create page's Archived tab restores them.
    archived: bool = False
    created_at: str
    updated_at: str
    last_built_at: str | None = None


class ApplicationCreate(BaseModel):
    name: str = Field(min_length=1)
    # Required: the description IS the brief the agent builds from.
    description: str = Field(min_length=1)
    agent_id: str = Field(min_length=1)
    icon: str | None = None
    # Free-form extra steer appended to the brief ("use a dark theme", "no
    # emoji"). Optional — the description alone is a valid brief.
    instructions: str = ""
    entrypoint: str = "index.html"


class ApplicationUpdate(BaseModel):
    # All optional; the route applies only explicitly-provided fields.
    name: str | None = None
    description: str | None = None
    icon: str | None = None
    entrypoint: str | None = None


class ApplicationBuildRequest(BaseModel):
    """Another build turn — "add a dark mode", "the header should stick"."""

    prompt: str = Field(min_length=1)


# --------------------------------------------------------------------------
# An application talking to an agent (app-agent-access.md §2). These are the
# app's API, not the SPA's: the caller is a page in the iframe or the app's
# own backend script.
# --------------------------------------------------------------------------


class AppAgentTurnRequest(BaseModel):
    """One message from an application to an agent."""

    message: str = Field(min_length=1)
    # What the app wants the agent to look at — the article, the selection,
    # the row. Kept separate from `message` so the agent can be told it's
    # material rather than instructions.
    context: str | None = None
    # Agent name. Omitted → the application's own agent, so the common case
    # needs no configuration.
    agent: str | None = None
    # Omitted → a new conversation; the id comes back in the response.
    conversation_id: str | None = None
    # Names a new conversation (ignored when continuing one).
    title: str | None = None


class AppAgentReply(BaseModel):
    conversation_id: str
    reply: str
    cost: float | None = None


class AppAgentInfo(BaseModel):
    """An agent as an application sees it: addressable by name, with nothing
    about how it's wired up."""

    name: str
    description: str = ""
    avatar: str = ""
    is_default: bool = False


class AppConversationMessage(BaseModel):
    role: str
    text: str | None = None
    seq: int


class AppConversation(BaseModel):
    id: str
    title: str
    agent_id: str | None = None
    created_at: str
    message_count: int = 0
    running: bool = False


class AppConversationDetail(AppConversation):
    messages: list[AppConversationMessage] = []
