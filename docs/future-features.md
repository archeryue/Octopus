# Future Features

## Session Persistence

**Priority**: High
**Status**: Done

SQLite persistence is implemented via `server/database.py` with write-through caching. Sessions and messages survive server restarts. Schema uses `sessions` and `messages` tables with cascade deletes.

---

## Session Handoff (Local Claude Code <-> Octopus)

**Priority**: High
**Status**: Done

Bi-directional session transfer between local Claude Code and Octopus:

- **`octopus handoff`** — Parses local Claude Code JSONL session files, maps message formats, and imports into Octopus via `POST /api/sessions/import`.
- **`octopus pull`** — Fetches a session from Octopus and writes it as a Claude Code JSONL file locally. Supports sessions without a `claude_session_id` (e.g. created via web UI) by generating a fresh UUID. Resume locally with `claude --resume <id>`.

---

## Telegram Bot Integration

**Priority**: Medium
**Status**: Planned

### Problem

The web UI requires a browser, which is not always convenient on mobile. A Telegram bot provides a lightweight, always-available interface to interact with Octopus sessions from any device.

### Goal

Connect to Octopus sessions and interact with Claude through Telegram — send messages, receive responses, and manage sessions without opening a browser.

### User Flow

1. Start a chat with the Octopus Telegram bot
2. Authenticate (e.g. `/login <token>` or a one-time link)
3. `/sessions` — list active sessions
4. `/use <session_id>` — attach to a session
5. Send messages directly — bot forwards to Claude and streams back the response
6. `/new <name>` — create a new session
7. Receive push notifications for long-running task completions

### Key Features

- **Session management**: list, create, delete, switch sessions via bot commands
- **Message relay**: send prompts and receive Claude's responses in chat
- **Streaming output**: use Telegram's edit-message API to simulate streaming
- **Approval handling**: when Claude needs tool approval, bot sends an inline keyboard (Approve / Deny)
- **Notifications**: push a message when a background task completes or Claude is waiting for input

### Proposed Approach

- Use `python-telegram-bot` (async, well-maintained) library
- Add a `TelegramBridge` service in `server/` that connects to `SessionManager`
- Bot authenticates users via the same token mechanism as the web UI
- Subscribe to session events via the existing broadcast/WebSocket system
- Config: `TELEGRAM_BOT_TOKEN` env var to enable the integration

### Architecture

```
Telegram <-> Bot Process <-> Octopus API <-> SessionManager <-> Claude SDK
                               (reuse existing REST + WS endpoints)
```
