# Octopus Architecture

Remote Claude Code controller — interact with Claude Code from any device via Web UI.

## System Overview

```
                    ┌──────────────┐
                    │   Web UI     │  React / Vite / TypeScript
                    │  :5173       │  zustand state, WebSocket client
                    └──────┬───────┘
                           │ WebSocket + REST
                    ┌──────▼───────┐
                    │  Octopus     │  FastAPI + uvicorn
                    │  Server      │  Python 3.12+
                    │  :8000       │
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
```

## Components

### Backend (`server/`)

| File | Purpose |
|---|---|
| `main.py` | FastAPI app, CORS middleware, route registration. Clears `CLAUDECODE` env var to allow nested subprocess spawning. |
| `config.py` | Pydantic settings loaded from `.env` — auth token, host, port, CORS origins, default working directory. |
| `auth.py` | Token verification for REST (`Authorization: Bearer <token>`) and WebSocket (`?token=<token>` query param). |
| `models.py` | Pydantic models for API requests/responses and internal message representation. |
| `session_manager.py` | Core component. Manages multiple Claude Code sessions via `claude-code-sdk`. Handles message streaming, tool result forwarding, and broadcast to connected WebSocket clients. |
| `routers/sessions.py` | REST CRUD — `GET/POST /api/sessions`, `GET/DELETE /api/sessions/{id}`. |
| `routers/ws.py` | WebSocket endpoint at `/ws`. Receives client commands (`send_message`, `approve_tool`, `deny_tool`), streams responses back. Each message send runs as a background `asyncio.Task` so the receive loop stays responsive. |

### Frontend (`web/src/`)

| File | Purpose |
|---|---|
| `App.tsx` | Root component. Token login screen, then layout with sidebar + main chat area. |
| `stores/sessionStore.ts` | Zustand store — token, sessions list, active session, messages per session, connection status. |
| `hooks/useWebSocket.ts` | WebSocket connection with auto-reconnect (3s interval). Uses `getState()` directly for store mutations to avoid React re-render loops. Dispatches incoming messages to the store by type. |
| `components/SessionList.tsx` | Sidebar — lists sessions, create/delete, select active. Fetches sessions via REST on mount. |
| `components/ChatView.tsx` | Main chat view — renders message list, text input, loading indicator. |
| `components/MessageBubble.tsx` | Renders a single message: user text, assistant markdown, collapsible tool use/result blocks, cost badges, errors. |
| `components/ToolApproval.tsx` | Renders Allow/Deny buttons when Claude requests tool permission (future use). |

## Data Flow

### Sending a message

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
        → ClaudeSDKClient.query("ls")
        → iterates receive_response() via _receive_safe()
        → yields events per SDK message:
            AssistantMessage → assistant_text / tool_use
            UserMessage (tool results) → skipped (echoed by SDK)
            ResultMessage → result (with cost, session_id)
            SystemMessage → skipped
      → broadcasts status: idle

Each yielded event sent to client via ws.send_json()
  → frontend handleWsMessage() dispatches to store
  → React re-renders ChatView with new messages
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
{"type": "result", "session_id": "xxx", "claude_session_id": "...", "cost": 0.03, "turns": 2}
{"type": "status", "session_id": "xxx", "status": "idle|running|waiting_approval"}
{"type": "error", "session_id": "xxx", "message": "..."}
```

### REST API

```
GET    /api/sessions              List all sessions
POST   /api/sessions              Create session {name, working_dir}
GET    /api/sessions/{id}         Session details + message history
DELETE /api/sessions/{id}         Delete session
GET    /health                    Health check
```

All REST endpoints require `Authorization: Bearer <token>`.

## Session Lifecycle

1. **Create** — `POST /api/sessions` creates a `Session` object with a UUID, name, and working directory. No Claude subprocess yet.
2. **First message** — `send_message` via WebSocket triggers `_run_claude()`, which creates a `ClaudeSDKClient`, connects, sends the prompt, and streams the response. The SDK spawns `claude` CLI as a subprocess.
3. **Conversation continuity** — After the first turn, `ResultMessage.session_id` is saved as `claude_session_id`. Subsequent messages pass this as `resume` in `ClaudeCodeOptions`, so Claude maintains conversation context.
4. **Concurrent sessions** — Each session gets its own `ClaudeSDKClient` instance (and thus its own CLI subprocess). Multiple sessions can run in parallel.
5. **Delete** — Disconnects the SDK client (kills subprocess) and removes the session from memory.

## Key Design Decisions

**SDK message parser patch**: The `claude-code-sdk` v0.0.25 throws `MessageParseError` on unknown message types like `rate_limit_event`, which kills the response stream. We patch `message_parser.py` to return a `SystemMessage` for unknown types instead of crashing.

**`CLAUDECODE` env var**: The Claude CLI refuses to start inside another Claude Code session. `main.py` clears this env var at import time so the SDK subprocess can launch.

**`bypassPermissions` mode**: Currently tools execute without approval prompts. The `ToolApproval` component and `can_use_tool` callback infrastructure exist for future interactive approval.

**`getState()` in WebSocket hook**: The `useWebSocket` hook uses `useSessionStore.getState()` directly instead of React selectors for store mutations. This avoids infinite re-render loops caused by zustand selector references changing on every render in React 19.

**Broadcast + direct send**: The server sends messages to the frontend via two channels — direct `ws.send_json()` in `_stream_response` for per-message events, and `session_manager._broadcast()` for status updates that go to all connected clients.

## Running

```bash
# Terminal 1 — Backend
.venv/bin/uvicorn server.main:app --host 0.0.0.0 --port 8000

# Terminal 2 — Frontend
cd web && bun dev --host 0.0.0.0
```

Open `http://localhost:5173`, enter the token from `.env` (`OCTOPUS_AUTH_TOKEN`), create a session, send a message.

## Tech Stack

- **Backend**: Python 3.12, FastAPI, uvicorn, pydantic-settings
- **Claude integration**: `claude-code-sdk` 0.0.25 (wraps Claude Code CLI subprocess)
- **Frontend**: React 19, TypeScript, Vite, zustand, react-markdown
- **Auth**: Token-based (`.env` config)
- **Communication**: WebSocket (streaming) + REST (CRUD)
