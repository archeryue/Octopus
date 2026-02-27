# Octopus

Remote controller for [Claude Code](https://docs.anthropic.com/en/docs/claude-code). Run Claude Code on your workstation, interact with it from your phone or any browser.

## How It Works

```
Browser (phone/tablet/desktop)
  → WebSocket → FastAPI server
    → claude-code-sdk → Claude Code CLI (local subprocess)
```

Octopus wraps your local Claude Code CLI via the official Python SDK. No extra API costs — it uses your existing Claude Code subscription.

## Quick Start

```bash
# Clone and setup
git clone https://github.com/archeryue/Octopus.git && cd Octopus
python3 -m venv .venv && .venv/bin/pip install -e "."
cp .env.example .env  # edit OCTOPUS_AUTH_TOKEN

# Frontend
cd web && bun install && cd ..

# Run
.venv/bin/uvicorn server.main:app --host 0.0.0.0 --port 8000
cd web && bun dev --host 0.0.0.0  # separate terminal
```

Open `http://localhost:5173`, enter your token, create a session, start chatting.

For phone access: use the same URL with your machine's LAN IP, or expose via tunnel.

## Features

- Multiple concurrent Claude Code sessions
- Real-time streaming responses via WebSocket
- Tool use display (collapsible command/result blocks)
- Conversation continuity (sessions resume across messages)
- Mobile-responsive dark UI
- Token-based auth

## Tech Stack

**Backend**: Python 3.12 / FastAPI / claude-code-sdk
**Frontend**: React 19 / TypeScript / Vite / zustand

## Testing

```bash
.venv/bin/pytest tests/ -v        # 23 backend tests
cd web && bun run test            # 8 frontend unit tests
cd web && bun run test:e2e        # 12 Playwright e2e tests
```

## Architecture

See [ARCHITECTURE.md](ARCHITECTURE.md) for detailed system design, data flow, and WebSocket protocol.
