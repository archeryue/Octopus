# Octopus Architecture

**Octopus is a personal agent platform.** It turns the local **Claude Code**
and **Codex** CLIs into durable, always-on agents you reach from a browser,
your phone, or Telegram. It drives the `claude` / `codex` CLIs directly via
their stream-JSON protocols — there is **no `claude-code-sdk` dependency** and
no extra per-token API cost beyond the CLI's own auth (your subscription or an
attached API key).

This doc describes the *current* system design. Per-initiative design history
lives in [`plans/`](plans/); CLI/stream-protocol research notes live in the
`*-notes.md` files. `server/database.py` (`_SCHEMA`) is the source of truth for
the data model — this doc describes it conceptually rather than pasting SQL.

## System Overview

```
        Phone / Browser / Telegram
              │  REST + WebSocket  │  bot long-poll
              ▼                    ▼
        ┌──────────────────────────────────┐
        │  FastAPI server (uvicorn) :8000   │  serves API + built SPA on one port
        │  ┌────────────┐  ┌──────────────┐ │
        │  │ REST routers│  │ BridgeManager│ │  Telegram (extensible ABC)
        │  │  + /ws      │  │              │ │
        │  └─────┬───────┘  └──────┬───────┘ │
        │        └──────┬──────────┘         │
        │        ┌──────▼───────┐            │
        │        │SessionManager│            │  one in-process turn engine
        │        └──────┬───────┘            │
        │        ┌──────▼───────┐            │
        │        │   Harness    │            │  RuntimeProfile per backend kind
        │        └──────┬───────┘            │
        └───────────────┼────────────────────┘
                        │ subprocess (stream-JSON over stdout)
            ┌───────────┴────────────┐
            │  claude --print …      │   or   codex exec --json …
            │  + injected MCP servers│        (bg · ask · connectors)
            └────────────────────────┘
                        │
            ┌───────────┴───────────┐
            │ Cloudflare Tunnel      │  (optional) public HTTPS via trycloudflare.com
            └────────────────────────┘
```

### Single-port serving

In production `octopus serve` serves everything from one port (default 8000):

- API routes (`/api/*`, `/ws`, `/health`) are FastAPI routers, registered first.
- Every other path serves the built SPA from `web/dist/` via
  `StaticFiles(html=True)` mounted at `/`.
- The frontend uses `window.location.origin` for REST and derives `ws://`/`wss://`
  from `window.location.protocol`, so it works behind any proxy, tunnel, or
  HTTPS terminator with no config.

In development, Vite serves the SPA on port 5173 with HMR and proxies `/api`,
`/ws`, `/health` to the backend on 8000.

## The agent model

An **Agent** is the durable definition of an assistant: name/avatar, system
prompt, model, default backend (`claude-code` | `codex`), an attached credential,
its MCP/tool set, tool allow/deny policy, and enabled connectors. Agents **own**
their Sessions, Schedules, and bridge bindings. A protected **Default Agent**
("Octo", `is_system=1`) always exists.

- A **Session** is one conversation thread (an instance of talking to an agent).
  It carries the backend resume id, working dir, origin (`user` | `schedule` |
  `bridge` | `delegation`), an optional `parent_session_id` (set when the
  session was spawned by another agent via `mcp__ask_agent__ask` — see
  [`plans/agent-collaboration.md`](plans/agent-collaboration.md)), and an
  `archived` flag.
- `SessionManager` reads the owning agent's config *directly each turn*, so
  editing an agent is picked up by its open sessions on their next turn (no
  restart, no re-bind).

## Components

### Backend (`server/`)

| File | Purpose |
|---|---|
| `main.py` | FastAPI app + lifespan. Clears the `CLAUDECODE` env var so a nested `claude` subprocess behaves normally. Wires DB, SessionManager, BridgeManager, ScheduleRunner, ConnectorManager, AgentManager, optional CloudflareTunnel. Registers routers, `GET /api/backends`, `GET /health`, and the SPA static mount. |
| `config.py` | Pydantic settings from `.env` (prefix `OCTOPUS_`) — see **Configuration** below. |
| `auth.py` | Bearer-token check for REST (`Authorization`) and WebSocket (`?token=`). |
| `crypto.py` | Fernet encryption (keyed off `OCTOPUS_AUTH_TOKEN`) for secrets at rest. |
| `models.py` | Pydantic request/response models + enums (`SessionStatus`, `MessageRole`, agent/schedule/connector/credential DTOs). |
| `session_manager.py` | Core turn engine. Owns in-memory `Session` objects, drives each turn through the **Harness**, persists + broadcasts events to WebSocket clients and bridges, runs tool-result forwarding, interactive questions, mid-turn interrupt, the per-session message queue, premature-exit auto-respawn, and large-prompt spill. |
| `harness/` | The single boundary for all model/runtime interaction (see below). |
| `agent_manager.py` | Agent CRUD (the durable assistant definitions). |
| `agent_memory.py` | Per-agent native memory provisioning (`<agents_dir>/<id>/memory/`). |
| `delegations.py` | Agent-to-agent delegation manager ([`plans/agent-collaboration.md`](plans/agent-collaboration.md)) — `mcp__ask_agent__ask` lets one agent spawn a child session under another agent, or continue a prior delegation in the same child session by passing its `delegation_id`. Subscribes to the session-manager broadcast bus; on the child's terminal event (`result` / `error` / `question_request`) injects an `[agent-reply:…]` / `[agent-error:…]` / `[agent-question:…]` turn back into the parent. Owns cycle + depth-3 guards (with DB fallback for archived ancestors), single-inject idempotency, same-session follow-up reset, cascade-cancel of descendants, and child auto-archive after terminal delivery. |
| `scheduler.py` | `ScheduleRunner` — APScheduler runner for recurring prompts, **interval and cron**, fired per agent into fresh auto-archiving sessions. |
| `schedule_ai.py` | Natural-language `/schedule` parsing — turns "every weekday at 9am" into a cron/interval spec via the agent's own harness (backend-agnostic). |
| `database.py` | SQLite (`aiosqlite`, WAL, FK cascade). `_SCHEMA` defines all tables; idempotent `_apply_migrations` / `_migrate_*` evolve existing DBs additively. |
| `jsonl_parser.py` / `jsonl_writer.py` | Read/write Claude Code JSONL session files for `octopus handoff` (import) and `octopus pull` (export → local `claude --resume`). |
| `bg_tasks.py` | Cross-turn background shell tasks: spawn, stream capture (bounded), idle watchdog, cancel, persistence in `bg_tasks`. |
| `large_prompts.py` | Spills any synthesized prompt over ~100 KB to a file and hands the model a small `Read` pointer (avoids `E2BIG` on argv). |
| `attachments.py` / `file_viewer.py` | Per-session upload cache; working-dir-scoped file reads for the viewer. |
| `connector_manager.py` + `connectors/` | Connector framework (see below). |
| `credentials` (`oauth_login.py`, `oauth_providers.py`, `oauth_errors.py`, `codex_login.py`) | Stored backend credentials: API keys, OAuth logins, and Codex device-auth in-app login. |
| `tunnel.py` | `cloudflared tunnel --url` subprocess manager; parses the public URL, monitors, stops gracefully. |
| `notifiers/` | Notification destinations (webhook), pluggable. |
| `fork_helpers.py` | Pure helpers for `/rewind`: git-anchor capture at turn-start, side-effect classification over parent rows, safe-revert preflight + git-stash execution. |
| `research/` | Native deep research orchestration (see below). |
| `cli.py` | `octopus serve` (`--tunnel`), `octopus handoff`, `octopus pull`. |

### Harness layer (`server/harness/`)

The harness is the **only** place that talks to a model runtime. There is one
`Harness` class and one `HarnessRun` subprocess engine, configured by a
`RuntimeProfile` **value** per backend kind — *no per-framework subclasses*
(design: [`plans/harness-layer.md`](plans/harness-layer.md)).

| File | Purpose |
|---|---|
| `harness.py` | The `Harness` front door (one per backend kind). |
| `profile.py` | `RuntimeProfile` — the per-backend data record + small collaborators (argv builder, event parser, capability flags). Capabilities (e.g. native memory, premature-exit recovery) are derived from the profile. |
| `run.py` | `HarnessRun` — universal subprocess + JSONL stream engine (4 MiB line limit, graceful shutdown, PATH discovery incl. nvm). |
| `assembly.py` | Shared per-turn assembly: MCP server selection, system-prompt composition (in-app tools blurb, connectors blurb, memory blurb), working-dir absolutization. |
| `events.py` | Backend-neutral `HarnessEvent` DTOs (`text`, `thinking`, `tool_use`, `tool_result`, `question_request`, `result`, `error`, `session_started`). |
| `claude_code.py` | Claude profile — `claude --print --output-format=stream-json` argv, event normalization, JSONL transcript codec, `run_oneshot`. |
| `codex.py` | Codex profile — `codex exec --json` argv (per-turn `-c` TOML overrides for MCP), event normalization, `run_oneshot`. Runs exactly once per turn. |
| `registry.py` | `get_harness(backend)` / `available_backends()` (a kind appears only when its CLI resolves on PATH; `claude-code` is always listed). |
| `login.py` | `LoginDriver` protocol for in-app credential login flows. |

### In-app MCP servers (`server/mcp_servers/`)

Every turn injects a small set of stdio MCP servers into the CLI's
`--mcp-config`. The built-ins are configurable per agent (default
`["ask", "bg", "ask_agent"]`); each enabled connector adds one more.

| Server | Tool(s) | Purpose |
|---|---|---|
| `bg.py` | `mcp__bg__run` / `cancel` / `list` | Fire-and-forget shell commands that run **across turns**; the result arrives as a follow-up turn. |
| `ask.py` | `mcp__ask__user(questions)` | Structured multiple-choice question rendered as a form in the UI; long-polls for the answer. Caller-aware: when invoked from a delegation-origin session the question is also injected as an `[agent-question:…]` turn into the parent agent's session — same pending queue, two answer producers. Replaces the old permission-prompt hack. |
| `ask_agent.py` | `mcp__ask_agent__ask` / `cancel` / `answer` / `list` | Agent-to-agent delegation. `ask(request, name=…, delegation_id=…, files=…)` is bimodal: pass `name` to spawn a fresh child session under the named agent, or pass `delegation_id` to continue a prior delegation in the same child session so the target keeps her transcript across rounds; exactly one id is required. `answer` drains a child's pending question with the parent's chosen label; `cancel` stops a delegation (cascade-cancels descendants); `list` enumerates this session's delegations. Same env-injection + bg-task-style follow-up-turn pattern as `bg`. Design: [`plans/agent-collaboration.md`](plans/agent-collaboration.md). |
| `connectors/github.py`, `gmail.py` | typed API tools | Built-in connector tools (see Connectors). |
| `connectors/custom.py` | generic `request(method, path, …)` | One tool for any user-defined OAuth2 API. |
| `connectors/_shared.py` | — | Token fetch+cache, 401→reconnect, 32 KB truncation. |
| `research.py` | `mcp__research__deep_research(question)` | Start a native deep-research job. Thin HTTP shim to `/api/sessions/{sid}/research`; returns `research_id` immediately so the model's turn ends cleanly. See Native Deep Research below. |

### Connector system (`server/connectors/` + `connector_manager.py`)

Connectors give an agent OAuth-authorized access to third-party APIs as MCP
tools. Enablement is **agent-scoped**; setup is **browser-only** (paste the
OAuth client id/secret in the UI — no env edit, no restart). The OAuth redirect
URI is derived from the incoming request, so it's correct behind a tunnel.
Built-ins: **GitHub**, **Gmail**. **Custom** kinds (authorize/token URLs, scopes,
PKCE, API base) are defined entirely from the browser and backed by a generic
OAuth provider + the generic `request` tool. See
[`connectors-setup.md`](connectors-setup.md) and [`plans/connectors.md`](plans/connectors.md).

Per-kind OAuth client creds live in `connector_oauth_clients`; installations in
`connector_installations` (+ split-secret `connector_installation_secrets`);
the agent↔installation join is `agent_connectors`; custom kinds in
`custom_connectors`. The manager handles install upsert, DB→env client-config
resolution, and server-side token refresh-on-near-expiry behind a per-install lock.

### Bridge system (`server/bridges/`)

Messaging-platform integrations behind an extensible `Bridge` ABC. Today:
Telegram (long-polling `getUpdates` — no webhook/SSL needed).

| File | Purpose |
|---|---|
| `base.py` | `Bridge` ABC + `TextBuffer` (aggregates streamed `assistant_text`, flushes on size/time) + the `handle_event` dispatcher and `QUIET_SUPPRESSED_EVENTS` policy. |
| `manager.py` | `BridgeManager` — routes inbound messages and slash commands, binds each chat to an **agent** with a sticky session + per-chat `verbose` flag, and fans SessionManager broadcasts back to the right chat. |
| `telegram.py` | `TelegramBridge` — long-poll loop, Markdown send + 4096-char splitting, inline keyboards (tool approval + the `/sessions` switch picker), rate-limit retry, `allowed_chat_ids` access control. |

A chat binds to an agent on first contact (Default Agent) with a sticky session
that rolls as threads come and go. **Quiet by default**: only the agent's
natural-language replies, errors, and approval prompts reach the chat;
`QUIET_SUPPRESSED_EVENTS` (`tool_use`, `tool_result`, `result`, `status`) are
hidden. `/verbose` and `/quiet` toggle this per chat (persisted in
`bridge_mappings.verbose`, preserved across `/agent` rebinds).

**Slash commands:** `/new [name]`, `/agent <name|id>`, `/sessions` (tappable
switch buttons), `/switch <id>`, `/current`, `/quiet`, `/verbose`, `/showme`
(intercepted with a "browser-only" notice — the viewer modal can't render in
Telegram), `/rewind` / `/fork` / `/research` (intercepted with a "browser-only"
notice — these require the browser UI), `/help`.

### Frontend (`web/src/`)

React 19 + TypeScript (strict) + Vite + zustand + Tailwind v4 + Radix.

| Area | Files |
|---|---|
| Shell | `App.tsx`, `components/AccountDropdown.tsx`, `OctopusLogo.tsx`, `SettingsDialog.tsx` |
| Agents | `AgentList.tsx` (two-pane sidebar: pick agent → see its sessions), `AgentSettings.tsx` (prompt/model/backend/credential/tools/connectors) |
| Sessions & chat | `SessionList.tsx` (sidebar with fork-tree disclosure; forks nest under root sessions), `ChatView.tsx` (virtualized via `react-virtuoso`, Enter-to-send, Esc-interrupt, queued-message badge, waiting-for-answer hint, per-user-message "Fork from here" button), `MessageBubble.tsx`, `SlashCommandMenu.tsx` (`/schedule`, `/remember`, `/research`, `/showme`, `/rewind`, `/fork`, `/archive`, `/reset` slash commands), `ForkDialog.tsx` (message picker + confirm popover with side-effect summary + optional git-revert checkbox), `ArchivedSessionsDialog.tsx` |
| In-app tools | `FileViewerDialog.tsx` (viewer), `BgTaskChip.tsx` (bg task status), `QuestionPrompt.tsx` (ask form), `ToolApproval.tsx` (approval prompt), `AgentDelegationRequestCard.tsx` (live status next to a `mcp__ask_agent__ask` tool_use), `AgentDelegationEventCard.tsx` (renders the `[agent-reply|question|error:…]` injected turns as collapsible cards with deep-links into the child session) |
| Schedules | `ScheduleList.tsx`, `SchedulesDialog.tsx` (all-agents overview) |
| Connectors / creds | `ConnectorList.tsx`, `CredentialList.tsx` |
| State | `stores/sessionStore.ts`, `hooks/useWebSocket.ts`, `hooks/useViewportHeight.ts` |

## Data Flow

### Sending a message (Web UI)

```
ChatView → useWebSocket.sendMessage()
  → optimistic user message in store
  → ws: {"type":"send_message","session_id","content","attachment_ids":[]}

ws.py receives → asyncio.create_task(stream)
  → SessionManager.start_message(session_id, content)
    → large_prompts.spill_if_large(...)        (only if huge)
    → broadcast status: running
    → Harness.run() spawns the agent's backend CLI with injected MCP servers
      → streams HarnessEvents: assistant_text / tool_use / tool_result /
        question_request / result
      → each event is persisted (messages) and broadcast to clients + bridges
    → broadcast status: idle
```

### Sending a message (Telegram, quiet by default)

```
TelegramBridge poll → _handle_update → BridgeManager.handle_incoming
  → slash command? handle it (/new, /sessions buttons, /switch, /quiet, …)
  → else: ensure chat is bound to an agent + sticky session, then start_message
SessionManager broadcasts events → BridgeManager._on_broadcast
  → drop QUIET_SUPPRESSED_EVENTS unless the chat is /verbose
  → bridge.handle_event → TextBuffer-batched replies, errors, approval prompts
```

### `/showme` — the in-app file viewer (browser only)

`/showme <reference>` is an **explicit user gesture** for opening a file in
the in-app viewer modal. ChatView intercepts it client-side, POSTs the raw
reference to `/api/sessions/{id}/showme/resolve`, and the resolver
(`server/showme_ai.py`) runs a one-shot model call (via the session's own
harness) that sees the last few messages of the conversation, returning JSON
with either `{"path"}` or `{"message"}`. On `path`, the client opens
`FileViewerDialog` directly — no model turn appears in the chat, no MCP tool
fires. Telegram intercepts `/showme` with a "browser-only" notice (the modal
can't render there). The agent is **never** instructed to open the viewer on
its own: it can't tell whether anyone is at the screen.

### Questions & tool approval

The `mcp__ask__user` MCP tool POSTs questions to
`/api/sessions/{id}/questions`; SessionManager broadcasts a `question_request`
(Web UI renders `QuestionPrompt`; Telegram surfaces it) and long-polls for the
answer, which is delivered back to the model. An unanswered question
auto-answers after `ask_user_question_timeout_seconds` so headless
bridge/scheduled sessions never wedge. (Native CLI tool-approval also exists via
`PendingApproval` futures + inline keyboards, but agents run with skip-permissions
by default and gate sensitive actions through `ask`/connector confirms instead.)

### Cross-turn background work

`mcp__bg__run` spawns a detached shell task (`bg_tasks.py`) that survives across
turns; when it exits, Octopus injects a follow-up turn carrying the captured
output (spilled to a file first if large). An idle watchdog SIGTERMs tasks that
go silent after producing output. Details: [`post-mortems/2026-05-18-bg-pipeline-hardening.md`](post-mortems/2026-05-18-bg-pipeline-hardening.md).

### Native Deep Research (`server/research/`)

`/research <question>` (or the `mcp__research__deep_research` MCP tool) starts a
multi-phase orchestrated research job managed entirely by Octopus — no dependence
on the Claude Code `/deep-research` workflow skill (which hangs inside a headless
turn). Design: [`plans/native-deep-research.md`](plans/native-deep-research.md).

**Pipeline phases (asyncio, all within Octopus):**

1. **Scope** — `run_oneshot` (tool-free) decomposes the question into 3–6 angles.
2. **Search + gather** — one scoped harness sub-turn per angle (parallel,
   semaphore-bounded), allowed only the harness's native web tools. The leaf
   executor (`research/leaf.py`) resolves the agent's credential, runs a throwaway
   `HarnessRun` with a minimal config (no MCP servers, no connectors, no memory dir),
   captures the final text as JSON findings, and discards the sub-turn.
3. **Dedup + rank** — pure Python over the returned `{claim, url}` findings.
4. **Verify** — top-ranked claims each get K independent web sub-turns
   (adversarial "try to refute"; majority-refute kills the claim).
5. **Synthesize** — `run_oneshot` merges survivors into a cited report.

**Concurrency and safety:** a per-job semaphore (configurable, default ~4–6) plus
a global cross-job cap (`research_max_concurrent_jobs`) bound all sub-turns. A
hard overall timeout and idle heartbeat prevent the hanging that plagued the CLI
Workflow. Cancellation reaps all in-flight `HarnessRun` subprocesses. A boot sweep
marks any `running` job `interrupted`.

**Delivery:** the report is written to `<working_dir>/research/<id>.md` and
injected into the session as a follow-up turn (`[deep-research:<id>] …report…`),
the same path bg-task delivery uses. A `ResearchCard` in the UI tracks phase
progress and exposes a cancel button.

**Backend-agnostic:** both Claude Code (`WebSearch`/`WebFetch`) and Codex
(`web_search`, enabled via `-c tools.web_search=true`) support research leaves.
A backend without web tools simply returns an "unavailable" message.

## WebSocket protocol (`/ws`)

**Client → Server**

```json
{"type":"send_message","session_id":"…","content":"…","attachment_ids":[]}
{"type":"interrupt","session_id":"…"}
{"type":"answer_question","session_id":"…","question_id":"…","answers":[…]}
{"type":"approve_tool","session_id":"…","tool_use_id":"…"}
{"type":"deny_tool","session_id":"…","tool_use_id":"…","reason":"…"}
```

**Server → Client**

```json
{"type":"user_message","session_id":"…","content":"…","attachments":[…]}
{"type":"assistant_text","session_id":"…","content":"…"}
{"type":"tool_use","session_id":"…","tool":"Bash","input":{…},"tool_use_id":"…"}
{"type":"tool_result","session_id":"…","tool_use_id":"…","output":"…","is_error":false}
{"type":"question_request","session_id":"…","question_id":"…","questions":[…]}
{"type":"result","session_id":"…","cost":0.03,"turns":2,"duration_ms":5000,"is_error":false}
{"type":"status","session_id":"…","status":"idle|running|waiting_approval"}
{"type":"error","session_id":"…","message":"…"}
{"type":"queued|dequeued","session_id":"…"}
{"type":"session_archived|session_unarchived","session_id":"…"}
```

## REST API

All endpoints require `Authorization: Bearer <token>`.

```
# Agents — durable assistant definitions that own sessions/schedules/bridges
GET/POST           /api/agents
GET/PATCH/DELETE   /api/agents/{id}
POST               /api/agents/{id}/archive
GET                /api/agents/{id}/sessions
GET                /api/agents/{id}/schedules
GET/POST/.../DELETE/api/agents/{id}/connectors        # per-agent enablement

# Sessions + per-session sub-resources
GET/POST           /api/sessions
GET/DELETE         /api/sessions/{id}
POST               /api/sessions/import
POST               /api/sessions/{id}/reset            # clear stuck-busy state
POST               /api/sessions/{id}/archive | /unarchive
POST/GET           /api/sessions/{id}/attachments[/{aid}]
GET                /api/sessions/{id}/files[/meta]
POST               /api/sessions/{id}/showme/resolve      # /showme reference → path
POST/GET           /api/sessions/{id}/bg-tasks[/{tid}][/cancel]
POST/GET           /api/sessions/{id}/questions[/{qid}/answer]
POST/GET           /api/sessions/{id}/delegations           # ask_agent: start, list
POST               /api/sessions/{id}/delegations/{did}/follow-up # ask_agent: continue same child session
POST               /api/sessions/{id}/delegations/{did}/cancel  # stop a delegation (cascades to descendants)
POST               /api/sessions/{id}/delegations/{did}/answer   # answer a child's question
POST               /api/sessions/{id}/fork                       # /rewind: fork conversation to a prior message
POST               /api/sessions/{id}/duplicate                  # /fork: filesystem copy onto a new working dir
POST               /api/sessions/{id}/research                   # /research: start a deep-research job
GET                /api/sessions/{id}/research                   # list jobs for this session
GET                /api/sessions/{id}/research/{rid}             # job detail + phase progress
POST               /api/sessions/{id}/research/{rid}/cancel      # cancel in-flight job

# Schedules (interval + cron), credentials, connectors, notifiers
GET/POST           /api/schedules
GET/PATCH/DELETE   /api/schedules/{id}
GET/POST/PATCH/DELETE  /api/credentials/...            # + OAuth + Codex login
GET                /api/connectors/catalog
PUT/DELETE         /api/connectors/{kind}/oauth-client
POST/GET/PATCH/DELETE /api/connectors[/{id}]
POST/DELETE        /api/connectors/custom[/{kind}]
GET/POST/PATCH/DELETE  /api/notifiers[/{id}]

GET                /api/backends                        # harnesses usable here
GET                /health                              # + per-bridge health
```

## Data model

`server/database.py` `_SCHEMA` is authoritative; this is the conceptual map.
SQLite, WAL, foreign-key cascade; additive `ALTER`s go through idempotent
migrations (never re-create or duplicate the schema in docs).

- **`agents`** — durable assistant definition (prompt, model, backend,
  credential, `mcp_servers`, tool allow/deny, `is_system`, `archived`). Owns the rest.
- **`sessions`** — one thread: `working_dir`, `claude_session_id` (backend
  resume id, name kept for back-compat), `agent_id`, `origin`
  (`user`|`schedule`|`bridge`|`delegation`|`fork`), `backend`, `credential_id`,
  `archived`. Delegation rows additionally carry `parent_session_id`
  (the caller session, `ON DELETE SET NULL` — orphaning beats
  mass-delete) and `delegation_request` (the verbatim original prompt
  for UI display) — see [`plans/agent-collaboration.md`](plans/agent-collaboration.md) §4.
  Fork rows additionally carry `forked_from_session_id` (the session that was
  branched), `fork_after_seq` (last copied message seq; the rewound message
  is at seq+1 on the parent), `fork_needs_replay` (Codex: wrap history into
  first turn), `fork_metadata` (ephemeral: prefilled prompt + first-turn note,
  cleared after first result), `fork_revert_record` (durable: git-stash outcome
  and stash ref), and `fork_status` (`initializing`|`reverting`|`ready`) for
  crash recovery — see [`plans/session-rewind.md`](plans/session-rewind.md) §4 and
  [`plans/session-fork.md`](plans/session-fork.md).
- **`messages`** — ordered history per session (`role`, `type`, `content`, tool
  fields, `cost`, `attachments`). User-message rows additionally record
  `git_head` and `git_status_clean` (captured at turn-start) so the fork
  safe-revert preflight can verify the working tree was clean at the branch point.
- **`schedules`** — recurring prompts owned by an agent: `interval_seconds` **or**
  `cron`+`timezone`, `recurrence_label`, `origin_session_id`, `enabled`.
- **`bridge_mappings`** — `(platform, chat_id)` → `agent_id` + sticky `session_id`
  (nullable) + `verbose`.
- **`backend_credentials`** + **`credential_secrets`** — credential metadata and
  its Fernet-encrypted secret, stored split; refresh/`needs_reconnect` lifecycle.
- **`connector_installations`** + **`connector_installation_secrets`**,
  **`agent_connectors`**, **`connector_oauth_clients`**, **`custom_connectors`** —
  the connector tables (see Connector system).
- **`notifiers`** — notification destinations.
- **`bg_tasks`** — cross-turn background task state (`status`, `exit_code`,
  captured stdio, timestamps).
- **`research_jobs`** — native deep-research job state: `question`, `status` (`running`|`completed`|`cancelled`|`failed`|`interrupted`), `phase`, `completed_at`, `injection_status` / `injected_at` / `delivery_error` (delivery tracked separately from completion so a job can be "done but report queued"), `report_path`, and per-phase progress counters. A boot sweep marks any `running` row `interrupted`.

## Memory

Each agent gets one canonical markdown dir, `<agents_dir>/<id>/memory/`, shared
by both backends (design: [`plans/memory.md`](plans/memory.md)):

- **Claude Code** points its auto-memory there via the
  `CLAUDE_COWORK_MEMORY_PATH_OVERRIDE` env var — **not** `CLAUDE_CONFIG_DIR`, so
  auth and `--resume` transcripts are untouched.
- **Codex** has no usable native memory in headless `exec`, so the dir is named
  in an injected `developer_instructions` blurb and the model reads/writes it
  with ordinary file tools. `CODEX_HOME` is untouched.

Memory is decoupled from both harnesses' config/auth dirs. The dir is
provisioned on agent create, kept on archive, removed on hard delete.

## Configuration (`OCTOPUS_*`)

| Setting | Default | Purpose |
|---|---|---|
| `auth_token` | — | Bearer token for all API/WS calls + Fernet key. |
| `host` / `port` | `0.0.0.0` / `8000` | Bind address. |
| `default_working_dir` | `.` | Working dir for new sessions. |
| `db_path` | `octopus.db` | SQLite file. |
| `attachments_dir` / `large_prompts_dir` / `agents_dir` / `codex_home_dir` | under `~/.octopus/` | Upload cache · large-prompt spill · agent memory roots · per-credential Codex auth. |
| `enable_tunnel` | `false` | Start a Cloudflare Tunnel. |
| `telegram_bot_token` / `telegram_allowed_chat_ids` / `telegram_api_base_url` | — | Telegram bridge (enabled when token set). |
| `ask_user_question_timeout_seconds` | `1800` | Auto-answer an unanswered question (so headless sessions don't wedge). |
| `public_base_url` | computed | Stable public host for connector OAuth redirect URIs (tunnel). |
| `gmail_/github_oauth_client_id`/`_secret` | — | Optional env fallback for connector OAuth clients (in-app config takes precedence). |
| `research_max_concurrent_jobs` | `2` | Max simultaneous deep-research jobs across all sessions. |

## Key design decisions

- **One harness, profiles not subclasses.** All model interaction goes through a
  single `Harness` + `HarnessRun` engine parameterized by a `RuntimeProfile`
  value per backend. Adding a backend is a new profile, not a new class tree.
- **CLIs, not an SDK.** Octopus spawns `claude --print` / `codex exec --json`
  and parses their stream-JSON itself (`harness/run.py`), so there's no
  `claude-code-sdk` dependency and behavior tracks the CLIs directly.
- **Agent-centric data model.** Sessions, schedules, and bridge bindings all
  hang off an agent; `SessionManager` reads agent config live each turn, so edits
  take effect on the next turn without restart.
- **Single-port, same-origin.** API + SPA on one port; the client derives all
  URLs from `window.location`, so tunnels/proxies/HTTPS work with zero config.
- **`ask` MCP over permission prompts.** Structured questions are an MCP tool
  with a UI form + long-poll + auto-answer timeout — robust for headless
  bridge/scheduled sessions where no human may be watching.
- **Quiet bridges.** Telegram chats see only the agent's replies by default;
  tool chatter is opt-in via `/verbose` (persisted per chat).
- **Session branching — two commands.** `/rewind` forks a
  conversation to any prior user message; the harness layer owns the per-backend
  resume strategy (both Claude and Codex use HISTORY_REPLAY: wrap the truncated
  transcript into the first turn's user-message channel; turn 2+ resumes natively).
  `/fork [name]` duplicates the current session onto an independent full copy of
  the working directory using a native transcript copy (Claude: copy the JSONL
  and rewrite session id + cwd; Codex: copy the rollout file) so the fork resumes
  with genuine context. Both commands produce `origin='fork'` session rows with a
  `forked_from_session_id` pointer; the sidebar builds a disclosure tree from this
  DAG. File revert on `/rewind` uses `git stash` gated on a strict preflight (clean
  tree at branch point, matching HEAD, only agent-touched files dirty). Designs:
  [`plans/session-rewind.md`](plans/session-rewind.md) and
  [`plans/session-fork.md`](plans/session-fork.md).
- **Per-turn safety net.** Every harness turn runs with a configurable idle
  timeout (`turn_idle_timeout_seconds`, default 300 s) and an overall cap
  (`turn_max_seconds`, default 1800 s). The child subprocess is spawned in its
  own process group (`start_new_session=True`) so `stop()` kills the whole
  group, not just the direct child. A timed-out turn surfaces a `turn_timeout`
  error and never enters premature-exit recovery or transient retry. Design:
  [`plans/turn-safety.md`](plans/turn-safety.md).
- **Three-tier failed-turn disposition.** After a turn fails, the run loop
  classifies the error: (1) auth-credential rejection (401/revoked/expired) →
  flag the bound credential `needs_reconnect` and stop — re-auth won't fix
  itself; (2) transient backend error (5xx/overloaded/dropped stream) →
  bounded exponential retry (max 2, resumes from captured session id when
  output was already streamed); (3) everything else (quota/credit/billing) →
  surface as-is. Classifiers are backend-declared pattern sets in
  `RuntimeProfile`. Designs: [`plans/harness-credential-reauth.md`](plans/harness-credential-reauth.md)
  and [`plans/harness-transient-retry.md`](plans/harness-transient-retry.md).
- **Hardened bg pipeline.** Large prompts spill to a file (`E2BIG` guard),
  premature CLI exit after a tool use auto-respawns once, and an idle watchdog
  reaps silent bg tasks. See [`post-mortems/2026-05-18-bg-pipeline-hardening.md`](post-mortems/2026-05-18-bg-pipeline-hardening.md).
- **Secrets split + encrypted.** Credential/connector secrets live in dedicated
  `*_secrets` tables, Fernet-encrypted with the auth token, read only by the
  MCP subprocess at tool-call time.
- **Schema evolves additively.** New tables go in `_SCHEMA`; column changes go
  through idempotent migrations — never an in-place destructive change.

## Running

```bash
# Production (single command)
cd web && bun run build && cd ..    # build the SPA (once)
octopus serve                       # API + UI on :8000
octopus serve --tunnel              # + public HTTPS via Cloudflare Tunnel

# With the Telegram bridge — add to .env, then `octopus serve`:
#   OCTOPUS_TELEGRAM_BOT_TOKEN=...
#   OCTOPUS_TELEGRAM_ALLOWED_CHAT_IDS=123,456     # optional access control

# Development (hot reload)
.venv/bin/uvicorn server.main:app --host 0.0.0.0 --port 8000 --reload   # backend
cd web && bun dev                                                        # frontend :5173

# CLI
octopus handoff [--session-id ID] [--name N]   # import a local Claude Code session
octopus pull SESSION_ID [--cwd DIR]            # export a session to local JSONL
```

## Tech stack

- **Backend**: Python 3.12 · FastAPI · uvicorn · pydantic-settings · aiosqlite
  (WAL) · APScheduler · cryptography (Fernet) · MCP stdio servers · httpx.
- **Model runtime**: `claude` + `codex` CLI subprocesses (stream-JSON) via the
  harness layer — no `claude-code-sdk`.
- **Frontend**: React 19 · TypeScript (strict) · Vite · zustand · Tailwind v4 ·
  Radix · react-markdown · react-virtuoso.
- **Tunnel**: Cloudflare Tunnel (optional, `cloudflared`).

## Tests

```bash
.venv/bin/pytest tests/ -v        # 882 backend (real-CLI tests run when claude/codex on PATH)
cd web && bun run test            # 84 frontend unit (vitest)
cd web && npx tsc --noEmit        # TypeScript check
cd web && bun run test:e2e        # 67 Playwright e2e (35 fast UI-only + 32 real-CLI @llm)
```
