# Octopus Architecture

Remote Claude Code controller — interact with Claude Code from any device via Web UI or messaging platforms.

## System Overview

```
                    ┌──────────────┐
                    │   Web UI     │  React / Vite / TypeScript
                    │  (built SPA  │  zustand state, WebSocket client
                    │   or dev)    │
                    └──────┬───────┘
                           │ same-origin (production)
                           │ or Vite proxy (development)
                    ┌──────▼───────┐
                    │  Octopus     │  FastAPI + uvicorn
                    │  Server      │  Serves API + static frontend
                    │  :8000       │
                    ├──────────────┤
                    │  Bridge      │  Messaging platform integrations
                    │  Manager     │  (Telegram, extensible to others)
                    └──────┬───────┘
                           │ claude-code-sdk
              ┌────────────▼────────────────┐
              │     Session Manager          │
              │  (multiple concurrent        │
              │   Claude Code sessions)      │
              └────────────┬────────────────┘
                           │ subprocess (via SDK)
                    ┌──────▼───────┐
                    │  Claude Code │
                    │  CLI         │
                    └──────────────┘
                           │
               ┌───────────┴───────────┐
               │ Cloudflare Tunnel     │  (optional)
               │ cloudflared subprocess│  public HTTPS via trycloudflare.com
               └───────────────────────┘
```

### Single-Port Architecture

In production, `octopus serve` serves everything from port 8000:
- API routes (`/api/*`, `/ws`, `/health`) are handled by FastAPI routers
- All other paths serve the built frontend from `web/dist/` via `StaticFiles(html=True)`
- The frontend uses `window.location.origin` for API calls and derives `ws://`/`wss://` from `window.location.protocol`, so it works behind any proxy, tunnel, or HTTPS terminator

In development, the Vite dev server runs on port 5173 with hot-reload and proxies `/api`, `/ws`, `/health` to the backend on port 8000.

## Components

### Backend (`server/`)

| File | Purpose |
|---|---|
| `main.py` | FastAPI app, CORS middleware, route registration, static file serving for built frontend. Clears `CLAUDECODE` env var to allow nested subprocess spawning. Initializes BridgeManager and CloudflareTunnel in the lifespan context. |
| `config.py` | Pydantic settings loaded from `.env` — auth token, host, port, CORS origins, default working directory, Cloudflare Tunnel toggle, Telegram bot config (token, allowed chat IDs, API base URL). |
| `auth.py` | Token verification for REST (`Authorization: Bearer <token>`) and WebSocket (`?token=<token>` query param). |
| `models.py` | Pydantic models for API requests/responses (`CreateSessionRequest`, `ImportSessionRequest`, `SessionInfo`, `SessionDetail`, `MessageContent`, `WsSendMessage`, `WsToolDecision`) and enums (`SessionStatus`, `MessageRole`). |
| `session_manager.py` | Core component. Manages multiple Claude Code sessions via `claude-code-sdk`. Handles message streaming, tool result forwarding, broadcast to connected WebSocket clients and bridges. Supports interactive tool approval via `PendingApproval` futures. Uses `_receive_safe()` to skip unparseable SDK messages. |
| `database.py` | SQLite persistence layer (`aiosqlite`). Three tables: `sessions`, `messages`, `bridge_mappings`. Write-through caching, WAL journal mode, foreign key cascading deletes. |
| `jsonl_parser.py` | Parses Claude Code JSONL session files — extracts metadata (session ID, cwd, first user message), converts message formats, consolidates multi-block messages (merges consecutive text, folds tool_result into tool_use). Filters by primary session ID (hint from filename or most-common count). |
| `jsonl_writer.py` | Writes Octopus sessions back to Claude Code JSONL format (with uuid chain, parentUuid links, version `2.1.62`, timestamps) for local resumption via `claude --resume`. |
| `tunnel.py` | CloudflareTunnel subprocess manager — starts `cloudflared tunnel --url`, parses the `trycloudflare.com` URL from stderr, monitors the process, and provides graceful stop (terminate with kill fallback). |
| `cli.py` | CLI entry point — `octopus serve` (default, checks for built frontend; `--tunnel` enables Cloudflare Tunnel), `octopus handoff` (import local session), `octopus pull` (export to local JSONL). |
| `routers/sessions.py` | REST CRUD — `GET/POST /api/sessions`, `GET/DELETE /api/sessions/{id}`, `POST /api/sessions/import`. |
| `routers/ws.py` | WebSocket endpoint at `/ws`. Receives client commands (`send_message`, `approve_tool`, `deny_tool`), streams responses back. Each message send runs as a background `asyncio.Task` so the receive loop stays responsive. |

### Bridge System (`server/bridges/`)

The bridge system provides messaging platform integrations, allowing users to interact with Claude Code sessions via external chat platforms.

| File | Purpose |
|---|---|
| `base.py` | Abstract `Bridge` base class and `TextBuffer`. Defines the interface all bridges must implement: `start`, `stop`, `send_text`, `send_tool_approval_request`, `send_tool_use`, `send_tool_result`, `send_status`, `send_result`, `send_error`. Includes event dispatcher (`handle_event`) that routes SessionManager events to the appropriate send method. TextBuffer aggregates streaming `assistant_text` chunks and flushes based on size (default 4096) or time (default 0.5s). |
| `manager.py` | `BridgeManager` — routes messages between messaging platforms and SessionManager. Maintains `platform:chat_id` → `session_id` mappings (persisted in SQLite). Handles slash commands (`/new`, `/sessions`, `/switch`, `/current`, `/help`). Forwards user messages to `SessionManager.send_message()` and routes session events back to the correct bridge and chat via broadcast subscription. Manages active stream tasks per chat. |
| `telegram.py` | `TelegramBridge` — Telegram Bot integration via long-polling (`getUpdates`). Sends messages with Markdown formatting, splits long text at 4096-char boundaries (preferring newline splits), sends inline keyboards for tool approval (Allow/Deny buttons with `approve:<id>`/`deny:<id>` callback data), typing indicators for running status, rate limit retry (429 with `retry_after`). Access control via `allowed_chat_ids`. |

### Frontend (`web/src/`)

| File | Purpose |
|---|---|
| `App.tsx` | Root component. Token login screen, then layout with sidebar + main chat area. Hamburger menu for mobile sidebar toggle with overlay. Logout button. |
| `stores/sessionStore.ts` | Zustand store — token (persisted to localStorage), sessions list, active session, messages per session, connection status. |
| `hooks/useWebSocket.ts` | WebSocket connection with auto-reconnect (3s interval). Derives WS URL from `window.location` (supports `ws://` and `wss://`). Uses `getState()` directly for store mutations to avoid React re-render loops. Handles all event types: `assistant_text`, `tool_use`, `tool_result`, `tool_approval_request`, `status`, `result`, `error`. |
| `components/SessionList.tsx` | Sidebar — lists sessions with status dots, create (name + optional working dir), delete, select active, copy session ID to clipboard. Uses `window.location.origin` for API calls. |
| `components/ChatView.tsx` | Main chat view — renders message list, text input (Enter to send, Shift+Enter for newline), loading indicator with animated dots, auto-scroll on new messages. Input disabled while session is running. |
| `components/MessageBubble.tsx` | Renders a single message: user text, assistant markdown (via react-markdown), collapsible tool use/result blocks with preview (command or file_path), cost badges, errors. |
| `components/ToolApproval.tsx` | Renders Allow/Deny buttons when Claude requests tool permission. Shows tool name and JSON-formatted input. |

### Build & Dev Config (`web/`)

| File | Purpose |
|---|---|
| `vite.config.ts` | Vite build config + dev server proxy (`/api`, `/ws`, `/health` → `localhost:8000`). Also configures vitest with jsdom environment. |
| `playwright.config.ts` | E2E test config — starts both backend and frontend dev servers, runs Chromium tests. Ignores `telegram-bridge.spec.ts` (separate config). |
| `playwright.bridge.config.ts` | Separate Playwright config for Telegram bridge E2E tests. Starts a fake Telegram API server on port 9999, then the backend with `OCTOPUS_TELEGRAM_BOT_TOKEN` and `OCTOPUS_TELEGRAM_API_BASE_URL` pointed at the fake server. |

## Data Flow

### Sending a message (Web UI)

```
User types "ls" → ChatView
  → useWebSocket.sendMessage()
    → adds user message to store (optimistic)
    → sends via WebSocket: {"type": "send_message", "session_id": "xxx", "content": "ls"}

Server receives in ws.py
  → asyncio.create_task(_stream_response(...))
    → session_manager.send_message(session_id, "ls")
      → yields user_message event
      → broadcasts status: running
      → _run_claude():
        → ClaudeSDKClient(options) as client
        → client.query("ls")
        → iterates _receive_safe(client):
            AssistantMessage → assistant_text / tool_use
            UserMessage (tool results) → tool_result events
            ResultMessage → result (with cost, session_id, duration_ms, is_error)
            SystemMessage → skipped
      → broadcasts status: idle

Each yielded event sent to client via ws.send_json()
  → frontend handleWsMessage() dispatches to store
  → React re-renders ChatView with new messages
```

### Sending a message (Telegram Bridge)

```
User sends "ls" in Telegram chat
  → TelegramBridge._poll_loop() receives update via getUpdates
  → _handle_update() extracts chat_id and text
  → checks allowed_chat_ids access control
  → BridgeManager.handle_incoming("telegram", chat_id, "ls", bridge)
    → checks for slash commands (none here)
    → looks up session_id from platform:chat_id mapping
    → asyncio.create_task(_stream_to_bridge(...))
      → session_manager.send_message(session_id, "ls")
      → for each event: bridge.handle_event(chat_id, event)
        → TextBuffer aggregates assistant_text chunks
        → flushes before tool_use/result/error events
        → sends tool_use as "*ToolName*: `preview`"
        → sends result as "*Done* ($0.0300)"
        → sends typing action for "running" status
```

### Tool Approval Flow

```
SessionManager._run_claude() encounters permission request
  → _make_permission_handler() creates PendingApproval with asyncio.Future
  → session status → waiting_approval
  → broadcasts tool_approval_request event
    → Web UI: ToolApproval component renders Allow/Deny buttons
    → Telegram: inline keyboard with approve/deny callback buttons
  → awaits future

User clicks Allow/Deny
  → Web UI: ws sends approve_tool/deny_tool → session_manager.approve_tool()
  → Telegram: callback_query → manager.handle_tool_decision()
  → future.set_result(PermissionResultAllow/Deny)
  → Claude continues or aborts tool execution
```

### WebSocket Protocol

**Client → Server:**
```json
{"type": "send_message", "session_id": "xxx", "content": "..."}
{"type": "approve_tool", "session_id": "xxx", "tool_use_id": "yyy"}
{"type": "deny_tool", "session_id": "xxx", "tool_use_id": "yyy", "reason": "..."}
```

**Server → Client:**
```json
{"type": "user_message", "session_id": "xxx", "content": "..."}
{"type": "assistant_text", "session_id": "xxx", "content": "..."}
{"type": "tool_use", "session_id": "xxx", "tool": "Bash", "input": {...}, "tool_use_id": "yyy"}
{"type": "tool_result", "session_id": "xxx", "tool_use_id": "yyy", "output": "...", "is_error": false}
{"type": "tool_approval_request", "session_id": "xxx", "tool_use_id": "yyy", "tool_name": "...", "tool_input": {...}}
{"type": "result", "session_id": "xxx", "claude_session_id": "...", "cost": 0.03, "turns": 2, "duration_ms": 5000, "is_error": false}
{"type": "status", "session_id": "xxx", "status": "idle|running|waiting_approval"}
{"type": "error", "session_id": "xxx", "message": "..."}
```

### REST API

```
GET    /api/sessions              List all sessions
POST   /api/sessions              Create session {name, working_dir}
GET    /api/sessions/{id}         Session details + message history
DELETE /api/sessions/{id}         Delete session
POST   /api/sessions/import       Import a session with messages
GET    /health                    Health check
```

All REST endpoints require `Authorization: Bearer <token>`.

## Session Lifecycle

1. **Create** — `POST /api/sessions` creates a `Session` object with a UUID, name, and working directory. Persisted to SQLite. No Claude subprocess yet.
2. **First message** — `send_message` via WebSocket or bridge triggers `_run_claude()`, which creates a `ClaudeSDKClient`, connects, sends the prompt, and streams the response. The SDK spawns `claude` CLI as a subprocess.
3. **Conversation continuity** — After the first turn, `ResultMessage.session_id` is saved as `claude_session_id`. Subsequent messages pass this as `resume` in `ClaudeCodeOptions`, so Claude maintains conversation context.
4. **Concurrent sessions** — Each session gets its own `ClaudeSDKClient` instance (and thus its own CLI subprocess). Multiple sessions can run in parallel. A per-session `asyncio.Lock` prevents concurrent sends to the same session.
5. **Import/Export** — `octopus handoff` imports a local Claude Code session; `octopus pull` exports an Octopus session as JSONL (with uuid chain, version metadata) for local resumption. Sessions without a `claude_session_id` get a generated UUID on pull.
6. **Delete** — Disconnects the SDK client (kills subprocess), removes from memory, and deletes from SQLite (cascade removes messages and bridge mappings).

## Bridge System

The bridge system enables interaction with Claude Code sessions via external messaging platforms.

### Architecture

```
BridgeManager (router + command handler)
  ├── maintains platform:chat_id → session_id mappings (SQLite-backed)
  ├── handles slash commands (/new, /sessions, /switch, /current, /help)
  ├── subscribes to SessionManager broadcasts
  └── registered bridges:
      └── TelegramBridge
          ├── long-polling via getUpdates (no webhook/SSL needed)
          ├── TextBuffer (4096 char limit, 0.5s flush delay)
          ├── inline keyboards for tool approval
          └── access control via allowed_chat_ids
```

### Slash Commands

| Command | Description |
|---|---|
| `/new [name]` | Create a new session (default name: "Bridge Session") |
| `/sessions` | List all sessions with status and current marker |
| `/switch <id>` | Switch to a different session |
| `/current` | Show current session info (name, status, messages, working dir) |
| `/help` | Show available commands |

### Configuration

Bridge configuration is via environment variables (prefixed `OCTOPUS_`):

| Variable | Purpose |
|---|---|
| `OCTOPUS_TELEGRAM_BOT_TOKEN` | Telegram Bot API token (enables bridge when set) |
| `OCTOPUS_TELEGRAM_ALLOWED_CHAT_IDS` | Comma-separated list of allowed Telegram chat IDs |
| `OCTOPUS_TELEGRAM_API_BASE_URL` | Override API URL (default: `https://api.telegram.org`, useful for testing) |

## Database Schema

```sql
-- Core session storage
CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    working_dir TEXT NOT NULL,
    created_at TEXT NOT NULL,
    claude_session_id TEXT
);

-- Message history (ordered per session)
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    role TEXT NOT NULL,
    type TEXT NOT NULL,
    content TEXT,
    tool_name TEXT, tool_input TEXT, tool_use_id TEXT,
    is_error INTEGER,
    session_id_ref TEXT,
    cost REAL
);
CREATE INDEX idx_messages_session ON messages(session_id, seq);

-- Bridge platform:chat → session mappings
CREATE TABLE bridge_mappings (
    platform TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    PRIMARY KEY (platform, chat_id)
);
```

## Key Design Decisions

**Single-port serving**: The FastAPI backend serves the built React frontend as static files via `StaticFiles(html=True)` mounted at `/`. API routes are registered before the static mount, so `/api/*`, `/ws`, and `/health` take priority. This means `octopus serve` is the only command needed — no separate frontend process.

**Same-origin URLs**: The frontend uses `window.location.origin` for REST and derives WebSocket protocol from `window.location.protocol`. This means the app works behind any proxy, tunnel, or HTTPS terminator without configuration. In dev mode, Vite's proxy config forwards API/WS requests to the backend.

**SDK message parser resilience**: The `claude-code-sdk` may throw `MessageParseError` on unknown message types (e.g. `rate_limit_event`), which would kill the response stream since Python generators are exhausted after an unhandled exception. `_receive_safe()` catches these and continues iteration, logging warnings for unparseable messages and errors.

**`CLAUDECODE` env var**: The Claude CLI refuses to start inside another Claude Code session. `main.py` clears this env var at import time so the SDK subprocess can launch.

**`bypassPermissions` mode**: Currently tools execute without approval prompts by default. The `ToolApproval` component (Web UI) and `PendingApproval` / `_make_permission_handler` infrastructure exist for interactive approval. The Telegram bridge supports approval via inline keyboard buttons.

**`getState()` in WebSocket hook**: The `useWebSocket` hook uses `useSessionStore.getState()` directly instead of React selectors for store mutations. This avoids infinite re-render loops caused by zustand selector references changing on every render in React 19.

**Broadcast + direct send**: The server sends messages to the frontend via two channels — direct `ws.send_json()` in `_stream_response` for per-message events, and `session_manager._broadcast()` for status updates and tool approval requests that go to all connected clients and bridges.

**Bridge text buffering**: Messaging platforms have rate limits and message length constraints. The `TextBuffer` aggregates streaming `assistant_text` events and flushes either when the buffer reaches `max_size` (4096 for Telegram) or after `flush_delay` (0.5s) of inactivity. Non-text events (tool_use, result, error) force an immediate flush to maintain ordering.

**Long-polling for Telegram**: The Telegram bridge uses `getUpdates` long-polling instead of webhooks. This avoids the need for a public URL or SSL certificate, making it work behind NAT/firewalls. The 30-second poll timeout balances responsiveness with resource usage.

**Bridge mapping persistence**: Platform chat-to-session mappings are stored in SQLite with foreign key cascade delete, so deleting a session automatically cleans up bridge mappings. Stale mappings (pointing to deleted sessions) are detected at message time and cleaned up with a user notification.

## Running

### Production (single command)

```bash
cd web && bun run build && cd ..   # build frontend (once)
octopus serve                       # serves everything on :8000
octopus serve --tunnel              # same, with Cloudflare Tunnel for public HTTPS
```

Open `http://localhost:8000`, enter the token from `.env` (`OCTOPUS_AUTH_TOKEN`), create a session, send a message.

### With Telegram Bridge

```bash
# Add to .env:
OCTOPUS_TELEGRAM_BOT_TOKEN=your-bot-token
OCTOPUS_TELEGRAM_ALLOWED_CHAT_IDS=123456789,987654321  # optional access control

octopus serve
```

Message your bot on Telegram: `/new My Session` then start chatting.

### Development (hot-reload)

```bash
# Terminal 1 — Backend
.venv/bin/uvicorn server.main:app --host 0.0.0.0 --port 8000 --reload

# Terminal 2 — Frontend (Vite proxies /api, /ws, /health → :8000)
cd web && bun dev
```

Open `http://localhost:5173` for the dev server with hot-reload.

### CLI Commands

```bash
octopus serve [--tunnel]                     # Start server (default command)
octopus handoff [--session-id ID] [--name N] # Import local Claude Code session
octopus pull SESSION_ID [--cwd DIR]          # Export session to local JSONL
```

## Tech Stack

- **Backend**: Python 3.12, FastAPI, uvicorn, pydantic-settings, aiosqlite
- **Claude integration**: `claude-code-sdk` 0.25+ (wraps Claude Code CLI subprocess)
- **Frontend**: React 19, TypeScript, Vite, zustand, react-markdown
- **Auth**: Token-based (`.env` config)
- **Persistence**: SQLite via aiosqlite (WAL mode, write-through caching)
- **Communication**: WebSocket (streaming) + REST (CRUD)
- **Bridges**: Telegram (long-polling via httpx), extensible Bridge ABC
- **Tunnel**: Cloudflare Tunnel (optional, via `cloudflared` subprocess)
- **Testing**: pytest (95 backend), vitest (8 frontend unit), Playwright (26 E2E across 3 specs)
