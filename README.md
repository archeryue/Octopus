# Octopus

Remote controller for [Claude Code](https://docs.anthropic.com/en/docs/claude-code). Run Claude Code on your workstation, interact with it from your phone, Telegram, or any browser.

## How It Works

```
Browser / Telegram Bot
  → WebSocket + REST / Bridge API → FastAPI server (serves UI + API on single port)
    → claude-code-sdk → Claude Code CLI (local subprocess)
```

Octopus wraps your local Claude Code CLI via the official Python SDK. No extra API costs — it uses your existing Claude Code subscription.

## Quick Start

```bash
# Clone and setup
git clone https://github.com/archeryue/Octopus.git && cd Octopus
python3 -m venv .venv && .venv/bin/pip install -e "."
cp .env.example .env  # edit OCTOPUS_AUTH_TOKEN

# Build frontend
cd web && bun install && bun run build && cd ..

# Run
octopus serve
```

Open `http://localhost:8000`, enter your token, create a session, start chatting.

For phone access: use `octopus serve --tunnel` for instant public HTTPS via Cloudflare Tunnel.

### Development Mode

For frontend hot-reload during development:

```bash
# Terminal 1 — Backend
.venv/bin/uvicorn server.main:app --host 0.0.0.0 --port 8000 --reload

# Terminal 2 — Frontend (proxies API/WS to backend)
cd web && bun dev
```

Open `http://localhost:5173` for the dev server with hot-reload.

## Features

- **Single-command server** — `octopus serve` serves both API and web UI on one port
- Multiple concurrent Claude Code sessions
- Real-time streaming responses via WebSocket
- Tool use display (collapsible command/result blocks)
- Tool approval (Allow/Deny) from Web UI or Telegram
- Conversation continuity (sessions resume across messages)
- SQLite persistence (sessions and messages survive restarts)
- **Long-running resilience** — Claude keeps running if the browser disconnects; state re-syncs on reconnect; session-owned tasks with lock timeout + `POST /api/sessions/{id}/reset` escape hatch
- Lazy-loaded message history (scales to long sessions without bloating memory)
- Auto-reconnecting database with batched commits per turn
- **Telegram Bot integration** — interact with Claude Code via Telegram (`/new`, `/sessions`, `/switch`); exponential backoff on API errors; per-bridge status in `/health`
- Session handoff: `octopus handoff` imports local Claude Code sessions
- Session pull: `octopus pull` exports sessions as JSONL for local `claude --resume`
- **Cloudflare Tunnel** — `octopus serve --tunnel` for instant public HTTPS
- HTTPS/WSS support (works behind tunnels and reverse proxies)
- Mobile-responsive dark UI
- Token-based auth

## CLI

```bash
octopus serve                  # Start server (API + UI on port 8000)
octopus serve --tunnel         # Start server with Cloudflare Tunnel (public HTTPS)
octopus handoff                # Import a local Claude Code session
octopus pull <session-id>      # Export an Octopus session as local JSONL
```

## Tech Stack

**Backend**: Python 3.12 / FastAPI / claude-code-sdk / aiosqlite
**Frontend**: React 19 / TypeScript / Vite / zustand

## Testing

```bash
.venv/bin/pytest tests/ -v        # 95 backend tests
cd web && bun run test            # 8 frontend unit tests
cd web && bun run test:e2e        # 26 Playwright e2e tests (app + handoff/pull + telegram bridge)
```

## Architecture

See [ARCHITECTURE.md](docs/ARCHITECTURE.md) for detailed system design, data flow, and WebSocket protocol.
