# Octopus

Remote controller for [Claude Code](https://docs.anthropic.com/en/docs/claude-code). Run Claude Code on your workstation, interact with it from your phone or any browser.

## How It Works

```
Browser (phone/tablet/desktop)
  → WebSocket + REST → FastAPI server (serves UI + API on single port)
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

For phone access: use the same URL with your machine's LAN IP, or expose via tunnel (see [deployment plan](docs/future-features.md#public-deployment)).

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
- Conversation continuity (sessions resume across messages)
- SQLite persistence (sessions and messages survive restarts)
- Session handoff: `octopus handoff` imports local Claude Code sessions
- Session pull: `octopus pull` exports sessions as JSONL for local `claude --resume`
- HTTPS/WSS support (works behind tunnels and reverse proxies)
- Mobile-responsive dark UI
- Token-based auth

## CLI

```bash
octopus serve                  # Start server (API + UI on port 8000)
octopus handoff                # Import a local Claude Code session
octopus pull <session-id>      # Export an Octopus session as local JSONL
```

## Tech Stack

**Backend**: Python 3.12 / FastAPI / claude-code-sdk / aiosqlite
**Frontend**: React 19 / TypeScript / Vite / zustand

## Testing

```bash
.venv/bin/pytest tests/ -v        # 86 backend tests
cd web && bun run test            # 8 frontend unit tests
cd web && bun run test:e2e        # 17 Playwright e2e tests
```

## Architecture

See [ARCHITECTURE.md](ARCHITECTURE.md) for detailed system design, data flow, and WebSocket protocol.
