# Octopus

**Octopus is a personal agent platform.** It turns **Claude Code** and **Codex**
into durable, always-on AI agents that run on your own machine and work for you
around the clock — reachable from your phone, any browser, or Telegram.

Each agent keeps its own persistent setup (prompt, model, tools, schedules,
connectors), keeps work running in the background across turns, and can reach
real third-party APIs. Octopus drives the `claude` / `codex` CLIs directly via
their stream protocols, so there's **no extra API cost** — it uses your existing
Claude and ChatGPT subscriptions (or an API key you attach).

## How It Works

```
Phone / Browser / Telegram
  → REST + WebSocket / bridge → FastAPI (web UI + API on one port)
      → Agent  (durable: prompt · model · credential · tool policy · connectors)
          → backend:  Claude Code   or   Codex      (local CLI subprocess, stream-json)
          → MCP tools: viewer · bg · ask · connectors (GitHub / Gmail / custom)
```

## Features

- **Agents** — Each agent is a durable assistant with its own system prompt,
  model, credential, tool policy, and connectors. Agents own their sessions,
  schedules, and bridge bindings; edit an agent and its open sessions pick up
  the change on the next turn. The sidebar is two-pane: pick an agent, see its
  sessions.
- **Two backends** — Run an agent on **Claude Code** or **Codex**, selectable
  per session. Same chat UX, schedules, bridges, and in-app tools either way.
- **Connectors** — Give agents OAuth access to third-party APIs as tools, set
  up entirely from the browser: built-in **GitHub** and **Gmail**, or define a
  **custom** connector for any OAuth2 API. Enabled per agent; client config +
  tokens encrypted at rest; the OAuth redirect URI is derived from your request
  so it works behind a tunnel. ([setup guide](docs/connectors-setup.md))
- **Credentials** — Store backend API keys / OAuth logins in-app, encrypted at
  rest (Fernet), and attach them per agent; falls back to the CLI's own login
  (`claude login` / `codex login`) when none is attached.
- **Run from anywhere** — One command serves the API and web UI on a single
  port; reach it from any browser or phone. `octopus serve --tunnel` gives
  instant public HTTPS via Cloudflare Tunnel. Token auth; HTTPS/WSS behind
  tunnels and reverse proxies.
- **Telegram** — Drive agents from a Telegram bot: each chat binds to an agent
  with a sticky session, `/sessions` lists threads as tappable switch buttons,
  and chats are **quiet by default** (only the agent's replies reach you —
  `/verbose` to also see tool activity). Allow/Deny tool-approval buttons;
  per-bridge status in `/health`.
- **Background & scheduled work** — Agents fire off shell commands that run in
  the background **across turns** (the result arrives as a follow-up turn), and
  recurring scheduled prompts run per agent into fresh, auto-archiving sessions.
- **In-app tools** — Every agent gets MCP tools Octopus injects: a file
  **viewer** (`/showme`), a **background runner**, and a structured
  **ask-the-user** prompt rendered as a multiple-choice form in the UI.
- **Built for long sessions** — Real-time WebSocket streaming with collapsible
  tool blocks; work keeps running if the browser disconnects and re-syncs on
  reconnect (with a `POST /api/sessions/{id}/reset` escape hatch); mid-turn
  interrupt (Esc) + message queue; virtualized, lazy-loaded chat that stays
  light on thousand-message sessions.
- **Local handoff** — `octopus handoff` imports local Claude Code sessions;
  `octopus pull` exports a session as JSONL for local `claude --resume`.
- **Persistence** — SQLite (WAL, batched commits per turn); sessions, messages,
  agents, credentials, connectors, and schedules survive restarts.

## Quick Start

```bash
# Clone and set up
git clone https://github.com/archeryue/Octopus.git && cd Octopus
python3 -m venv .venv && .venv/bin/pip install -e "."
cp .env.example .env          # edit OCTOPUS_AUTH_TOKEN

# Build the frontend (the server serves web/dist/)
cd web && bun install && bun run build && cd ..

# Run (API + UI on port 8000)
octopus serve
```

Open `http://localhost:8000`, enter your token, pick the default **Octo** agent,
create a session, and start chatting. For phone access, `octopus serve --tunnel`
gives you a public HTTPS URL.

Auth for the agent's backend uses your existing CLI login (`claude login` /
`codex login`) by default — or add an API key under **Credentials** and attach
it to an agent.

### Development mode

```bash
# Terminal 1 — backend (hot-reload)
.venv/bin/uvicorn server.main:app --host 0.0.0.0 --port 8000 --reload

# Terminal 2 — frontend (Vite proxies /api + /ws to the backend)
cd web && bun dev
```

Open `http://localhost:5173` for the hot-reloading dev server.

## CLI

```bash
octopus serve                  # Start server (API + UI on port 8000)
octopus serve --tunnel         # ... with a public Cloudflare Tunnel (HTTPS)
octopus handoff                # Import a local Claude Code session
octopus pull <session-id>      # Export an Octopus session as local JSONL
```

## Tech Stack

**Backend**: Python 3.12 · FastAPI · `claude` + `codex` CLI subprocesses ·
aiosqlite · APScheduler · cryptography (Fernet) · MCP stdio servers
**Frontend**: React 19 · TypeScript (strict) · Vite · zustand · Tailwind v4 · Radix

## Testing

```bash
.venv/bin/pytest tests/ -v        # 668 backend tests (real-CLI tests run when `claude`/`codex` on PATH)
cd web && bun run test            # 45 frontend unit tests (vitest)
cd web && npx tsc --noEmit        # TypeScript check
cd web && bun run test:e2e        # 61 Playwright e2e tests (app · handoff/pull · telegram · agents · connectors · real-CLI)
```

### Pre-commit hooks (optional)

Install [lefthook](https://github.com/evilmartians/lefthook) and run
`./scripts/setup-hooks.sh` to enable per-commit checks: `tsc --noEmit` when web
TS changes, `pytest tests/` when server Python changes. Both skip when no
relevant files are staged, so doc-only commits stay fast.

## Architecture

See [docs/](docs/) for all documentation — start with
[docs/architecture.md](docs/architecture.md) for system design, data flow, and
the WebSocket protocol; [docs/plans/](docs/plans/) holds the per-initiative
design records, and [docs/future-features.md](docs/future-features.md) tracks
deliberately-deferred work.
