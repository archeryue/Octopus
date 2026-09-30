# Octopus

**Octopus is a personal agent platform.** It turns **Claude Code** and **Codex**
into durable, always-on AI agents that run on your own machine and work for you
around the clock — reachable from your phone or any browser.

One install can serve more than one person. Each **account** is a completely
separate Octopus — its own agents, sessions, applications, credentials, memory,
and a workspace it cannot see out of — so a household or a few trusted people
share the machine without sharing anything else. Signup is invite-only, and the
first account (created with the install's own token) is the admin who mints the
invites. It stays a *personal* product either way: there is no cross-account
collaboration, no shared team space — just several private ones side by side.

Each agent keeps its own persistent setup (prompt, model, tools, schedules,
connectors), keeps work running in the background across turns, and can reach
real third-party APIs. Octopus drives the `claude` / `codex` CLIs directly via
their stream protocols, so there's **no extra API cost** — it uses your existing
Claude and ChatGPT subscriptions (or an API key you attach).

## How It Works

```
Phone / Browser
  → REST + WebSocket → FastAPI (web UI + API on one port)
      → Agent  (durable: prompt · model · credential · tool policy · connectors)
          → backend:  Claude Code   or   Codex      (local CLI subprocess, stream-json)
          → MCP tools: bg · ask · ask_agent · schedule · research · connectors
                       (served in-process at /mcp/<name>, not spawned per session)
```

## Features

- **Agents** — Each agent is a durable assistant with its own system prompt,
  model, credential, tool policy, and connectors. Agents own their sessions,
  and schedules; edit an agent and its open sessions pick up
  the change on the next turn. The sidebar is two-pane: pick an agent, see its
  sessions.
- **Two backends** — Run an agent on **Claude Code** or **Codex**, selectable
  per session. Same chat UX, schedules, and in-app tools either way.
- **Connectors** — Give agents OAuth access to third-party APIs as tools, set
  up entirely from the browser: built-in **GitHub** and **Gmail**, or define a
  **custom** connector for any OAuth2 API. Enabled per agent; client config +
  tokens encrypted at rest; the OAuth redirect URI is derived from your request
  so it works behind a tunnel. ([setup guide](docs/connectors-setup.md))
- **Credentials** — Store backend API keys / OAuth logins in-app, encrypted at
  rest (Fernet, under your account's key), and attach them per agent; falls back
  to the CLI's own login (`claude login` / `codex login`) when none is attached.
- **Accounts & isolation** — Multiple people on one install, each fully
  isolated. Accounts sign in with a username + password (scrypt-hashed);
  signup is **invite-only** (admins mint codes with a use count and TTL). Every
  owned row — sessions, agents, applications, credentials, connectors, schedules
  — carries a `user_id`, so one account never sees another's data, and each
  account's secrets are encrypted under a **per-user key** so even the database
  can't cross the line. A session's working directory is **confined** to the
  account's own workspace (a relative path means "inside my workspace"; admins
  can grant extra roots), and each account gets its own concurrency quota.
  Admin surface: invites, disable/enable people, operator-only Monitor. Before
  the first account exists the install runs on its **install token**; creating
  that first account adopts whatever is already there.
  Design: [`docs/plans/multi-tenancy.md`](docs/plans/multi-tenancy.md).
- **Run from anywhere** — One command serves the API and web UI on a single
  port; reach it from any browser or phone. `octopus serve --tunnel` gives
  instant public HTTPS via Cloudflare Tunnel. Session-bearer auth that a browser
  or phone remembers for 30 days; HTTPS/WSS behind tunnels and reverse proxies.
- **Background & scheduled work** — Agents fire off shell commands that run in
  the background **across turns** (the result arrives as a follow-up turn), and
  recurring scheduled prompts run per agent into fresh, auto-archiving sessions.
- **Agent-to-agent collaboration** — One agent can delegate to another by
  name: "ask Vera to review this file" spawns Vera in her own session
  under her own agent config (credentials, memory, tools, even a different
  backend — `claude-code` agent can delegate to a `codex` agent), and her
  reply lands back in the caller's session as a follow-up turn. The
  model:
  - **The principal-chain rule.** Every session has exactly one *caller*
    (the human for root sessions, the parent agent's session for
    delegations). Questions and replies always travel one hop — to the
    caller. If Vera doesn't know something, she asks her caller; if the
    caller is another agent and *it* doesn't know, it asks *its* caller;
    only the top of the chain (the human) ever sees a question that
    propagated all the way up.
  - **Async by default**, like background tasks. The model calls
    `mcp__ask_agent__ask`, gets a `delegation_id` immediately, ends its
    turn; the reply (`[agent-reply:Vera …]`) arrives as a new turn when
    Vera finishes. Multiple delegations in flight at once give you
    parallel fan-out for free.
  - **Same-session follow-ups** for iteration. Calling `ask` again with a
    prior `delegation_id` continues that child session, so the delegated
    agent keeps her own transcript for review rounds instead of starting
    cold.
  - **Cascade-cancel + nested chains.** Octo → Vera → Pete is supported
    (depth-3 cap with cycle detection); cancelling Vera also cancels
    Pete. Chat UI renders three card types — the in-flight delegation,
    the reply when it lands, and a question that travelled back to you
    — and the sidebar surfaces hidden delegation sessions on demand.
  - Design: [`docs/plans/agent-collaboration.md`](docs/plans/agent-collaboration.md).
- **In-app tools** — Every agent gets MCP tools Octopus injects: a
  **background runner**, a structured **ask-the-user** prompt rendered as
  a multiple-choice form in the UI, and the agent-to-agent **delegation**
  tools described in the previous bullet.
- **In-app file viewer** — `/showme <reference>` opens a file from the
  session's working directory in a browser modal — markdown rendered,
  images/PDFs inline, code highlighted. Exact paths short-circuit (no
  model call); fuzzy references like `the readme` are resolved by a
  one-shot model call that reads recent conversation. Browser-only by
  design — the agent never opens files on its own, since it can't tell
  whether anyone is at the screen.
- **Built for long sessions** — Real-time WebSocket streaming with collapsible
  tool blocks; work keeps running if the browser disconnects and re-syncs on
  reconnect (with a `POST /api/sessions/{id}/reset` escape hatch); mid-turn
  interrupt (Esc) + message queue; virtualized, lazy-loaded chat that stays
  light on thousand-message sessions.
- **Session branching** — Two commands for exploring alternatives without
  losing context:
  - **`/rewind`** — rewind a conversation to any prior user message and
    re-issue it (edited, redone, or replaced). A sidebar tree shows branches; a
    confirm popover discloses side effects (file edits, bg tasks, irreversible
    tool calls) and optionally reverts file edits via `git stash` when the
    working tree is clean. Design:
    [`docs/plans/session-rewind.md`](docs/plans/session-rewind.md).
  - **`/fork [name]`** — duplicate the **current** session onto an independent
    full copy of its working directory, leaving the original untouched. The fork
    resumes the real conversation history (native transcript copy), so the model
    sees full context. Design:
    [`docs/plans/session-fork.md`](docs/plans/session-fork.md).
- **Native deep research** — `/research <question>` (or the
  `mcp__research__deep_research` MCP tool, which agents can call themselves)
  starts a multi-phase Octopus-orchestrated research job: scope decompose →
  parallel web-search sub-turns per angle → dedup + rank → adversarial verify →
  final synthesis into a cited report. Octopus owns the fan-out,
  concurrency limits, cancellation, and persistence; web access comes from the
  harness's own native tools (`WebSearch`/`WebFetch` for Claude; `web_search` for
  Codex). A `ResearchCard` in the chat tracks phase progress and exposes a cancel
  button; the final report arrives as a follow-up turn the agent can act on.
  Design: [`docs/plans/native-deep-research.md`](docs/plans/native-deep-research.md).
- **Applications** — Ask an agent to build you a web app and it lands in the
  sidebar. Describe what you want, pick the agent, and it writes a
  self-contained static site into a directory Octopus owns inside your account's
  workspace; the moment the entry page exists, the app
  renders in the main pane like a browser tab. Ask for changes right from that
  pane — each request is another turn in the same build session, so the agent
  keeps its context and the frame reloads itself when the rebuild lands. An app
  that needs a server gets one: drop an executable `start.sh` at its root and
  Octopus allocates a port, runs it, proxies `/apps/{id}/api/*` to it and stops
  it when idle — with the app's own state in a directory a rebuild never
  touches. A running app can also hold conversations with your agents through
  its own scoped token.
  Design: [`docs/plans/applications.md`](docs/plans/applications.md),
  [`application-backends.md`](docs/plans/application-backends.md),
  [`app-agent-access.md`](docs/plans/app-agent-access.md).
- **Per-agent memory** — Each agent has its own memory directory that both
  harnesses write to and read back across sessions, so an agent accumulates what
  it learned about your setup instead of starting cold every time. Claude's
  native auto-memory is pointed at it; Codex is told where it is. Auth and
  `--resume` transcripts are untouched by this.
  Design: [`docs/plans/memory.md`](docs/plans/memory.md).
- **Native sub-agents, surfaced** — When a model spawns its own helpers
  (Claude Code's `Task`, Codex's `spawn_agent`), the run shows up as a live card
  with status and token counts rather than a tool call that sits there. An
  Octopus agent can also bring its own named sub-agents.
  Design: [`docs/plans/native-subagents.md`](docs/plans/native-subagents.md).
- **Monitor** — A page for what the system actually did: request and turn
  counts, error rates, per-process memory (PSS, so shared pages are not counted
  twice), database and WAL size. Sampled continuously, kept 30 days, and an
  empty section says "nothing recorded" rather than rendering blank — a blank
  panel reads as a clean bill of health.
- **Encryption at rest** — Secrets ride a key hierarchy: a server-held **master
  key** wraps each account's data-encryption key (DEK), which encrypts that
  account's credentials and connector tokens. A password only authenticates —
  changing it re-wraps just that account's DEK, and dropping the DEK
  crypto-shreds the account (its ciphertext becomes noise). Install-era secrets,
  from before the first account, are keyed by the access token instead.
- **Token rotation** — The access token is the credential clients present *and*
  the key for those install-era secrets, so changing it is one operation from
  Settings (`POST /api/auth/rotate`), with no restart and nothing to edit by
  hand: editing `.env` yourself changes what the server checks while leaving
  those secrets keyed to a token nobody has. Rotation re-keys them, rewrites the
  env files, swaps the live setting, drops the processes carrying the old token,
  and hands the new one to tabs already open.
  Design: [`docs/plans/token-rotation.md`](docs/plans/token-rotation.md).
- **Local handoff** — `octopus handoff` imports local Claude Code sessions;
  `octopus pull` exports a session as JSONL for local `claude --resume`.
- **Persistence** — SQLite (WAL, batched commits per turn); accounts, invites,
  sessions, messages, agents, credentials, connectors, and schedules survive
  restarts.

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

Open `http://localhost:8000` and enter the install token from your `.env`. On a
fresh install that first token gets you to **Create the first account** —
pick a username and password, and that account becomes the admin (it also
adopts any agents/sessions already on disk). From then on you sign in with that
username and password, and the admin mints **invite codes** for anyone else who
should have their own account. Then pick the default **Octo** agent, create a
session, and start chatting. For phone access, `octopus serve --tunnel` gives
you a public HTTPS URL.

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
aiosqlite · APScheduler · cryptography (Fernet) · MCP namespaces served
in-process over streamable-HTTP
**Frontend**: React 19 · TypeScript (strict) · Vite · zustand · Tailwind v4 · Radix

## Testing

```bash
./scripts/check.sh                # every gate: ruff · mypy · pytest · eslint · vitest · tsc · contracts · docs index

.venv/bin/pytest -m "not real"    # 1,313 backend tests, hermetic — no CLI, no network, ~34s
.venv/bin/pytest -m real          # the 34 that drive a live model (needs a signed-in claude / codex)
cd web && bun run test            # 299 frontend unit tests (vitest)
cd web && bun run typecheck       # TypeScript check (tsc -b across the project)
cd web && bun run test:e2e        # 107 Playwright tests in a real browser
cd web && bun run test:e2e:fast   # ... the 67 that need no model (~40s)
cd web && bun run test:e2e:llm    # ... the 40 that drive real Claude / Codex turns
```

The real-CLI tier is selected by marker rather than by filename, so a plain
`--collect-only` never spawns a model.

### Pre-commit hooks (optional)

Install [lefthook](https://github.com/evilmartians/lefthook) and run
`./scripts/setup-hooks.sh`. Commits then run the same gates `./scripts/check.sh`
does, scoped to what you staged: ruff + mypy + the hermetic pytest tier for
Python, eslint + `tsc` + vitest for web, and the generated-contract and
docs-index checks when they could have drifted. Each skips when no relevant file
is staged, so a doc-only commit stays fast.

## Architecture

See [docs/](docs/) for all documentation — start with
[docs/architecture.md](docs/architecture.md) for system design, data flow, and
the WebSocket protocol; [docs/plans/](docs/plans/) holds the per-initiative
design records.
