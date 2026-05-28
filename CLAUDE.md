# Octopus Development Rules

## Do It Right The First Time (no MVPs, no future-polish)

**If we choose to do something, we do it perfectly — right now, in
this session.** No "minimal fix", no "MVP for now", no "we'll polish
this later". No deferral of cleanup to a "follow-up" item.

This rule is non-negotiable. Specifically that means:

- Never ship a half-done implementation and document the rest as
  "future work". If the full thing isn't worth doing right now, then
  don't start it at all.
- Never write the cleaner version into `docs/future-features.md`
  *instead* of doing it. The doc is for things we genuinely choose
  not to do now (because they need a real second use case, an
  external dep, a user decision); it is not a parking lot for
  "felt too long".
- Never add a comment like `# TODO: handle X properly later` or `#
  HACK: works for now`. If `X` matters, handle it in this change.
  If it doesn't matter, delete the comment.
- When the user asks "fix this", interpret it as "fix it the way a
  careful engineer with infinite time would" — not "ship the
  smallest patch that no longer crashes".
- "MVP" is not a status the user has to accept. There is no future
  in which a later session will go back and polish; in the AI era
  we have the bandwidth to do it right *now*, here, in one go.

This rule exists because past sessions repeatedly took the shortcut
and then had to be told to go back and do the real thing. Skip the
shortcut. Do the real thing the first time.

## After Every Code Change

You MUST verify your changes before considering them done:

1. **Backend unit tests**: `.venv/bin/pytest tests/ -v` (668 tests; the
   real-CLI tests auto-skip unless their binary is on PATH —
   `test_backend_claude_code_real.py` + `test_schedule_ai_real.py` need
   `claude`; `test_backend_codex_real.py` + `test_codex_login_real.py` need
   `codex`; `test_agent_memory_real.py` needs **both** — run with the nvm bin
   prepended, see Conventions)
2. **Frontend unit tests**: `cd web && bun run test` (45 tests)
3. **TypeScript check**: `cd web && npx tsc --noEmit`
4. **E2E tests**: `cd web && bun run test:e2e` (61 tests, Playwright auto-starts servers)

**Zero test failures are acceptable.** All tests must pass before committing. If a test fails, investigate and fix it — do not ignore, skip, or dismiss any failure as "flaky" or "pre-existing".

## Test Coverage

| Suite | Tool | Count | What it covers |
|-------|------|-------|----------------|
| Backend unit | pytest | 668 | Config, models, session manager, REST API (auth, CRUD, 404s, reset), database persistence (incl. credential storage split + refresh-error codes), JSONL parser/writer, CLI (handoff, pull), import API, schedules CRUD + scheduler runner (interval **and cron** triggers) + schedule-recurrence migration, **natural-language `/schedule` parsing** (`schedule_ai`: rigid fast-path, JSON extraction, cron/interval validation, `from_text` route — harness-agnostic, runs on the agent's own harness claude-code **or** codex, with fake + real-CLI AI), bridge base/manager/telegram (incl. **per-chat verbosity** — quiet by default, hiding tool/result/status events, `/quiet`+`/verbose` toggle persisted through `/agent` rebinds — and **session-switch via inline buttons**: `/sessions` renders a tappable picker, shared `switch_session` for the `switch:<id>` callback + `/switch` command), tunnel config, OAuth provider registry, agents (manager + routes), **harness layer** (`harness`: one `Harness` + one `HarnessRun` subprocess engine driven by a per-backend `RuntimeProfile` value — no per-framework subclasses; shared MCP/system-prompt assembly, registry + derived predicates, `run_oneshot` for both backends, claude + codex argv/parser snapshots, real-CLI for both when on PATH), **Codex in-app login** (`codex_login` device-auth orchestrator with a fake CLI: scrape URL+code, success/fail/cancel, per-credential `CODEX_HOME` resolution via `resolve_credential_by_id(style="home_dir")`, `/credentials/codex/*` routes; real-CLI start when `codex` on PATH), **connectors** (DB split-secret + agent-join, manager incl. token-refresh lifecycle + in-app OAuth-client config + custom-connector CRUD, OAuth providers, REST routes incl. OAuth flow, the github/gmail/generic MCP servers), **agent memory** (`agent_memory`: per-agent native-memory path derivation + idempotent provisioning; harness wiring giving Claude a `CLAUDE_COWORK_MEMORY_PATH_OVERRIDE` (never `CLAUDE_CONFIG_DIR` — resume-transcript regression guard) and Codex a memory-dir blurb in `developer_instructions` *without* enabling `features.memories`; agent-manager dir provision/cleanup; gated real-CLI read-back for both harnesses **plus a Claude `--resume`-survives-memory-override** check) |
| Frontend unit | vitest | 45 | Zustand store (token, sessions, messages, status, agents, connectors), useWebSocket, BgTaskChip, FileViewerDialog, SlashCommandMenu |
| E2E | Playwright | 61 | Login, session CRUD, real Claude responses (incl. AskUserQuestion + resume), Enter to send, input/state while running, WebSocket reconnect, mobile layout, CLI handoff/pull + roundtrip + API cleanup, Telegram bridge (fake API server — quiet-mode octo replies, `/sessions` switch buttons, `/quiet`+`/verbose` toggles), schedules (`/schedule` command → all-agents overview dialog, toggle/delete), archived-sessions account-menu manage page (view read-only + unarchive), waiting-input hint, message queue + Esc interrupt, virtualized chat scrolling, OAuth dialog flow (Claude Code + **Codex device-code sign-in** via the Harness chooser), credential override, agents rail/settings, **connectors** (catalog + availability gating, in-app Set-up flips a built-in to connectable, add/remove a custom connector, per-agent toggles) |

## Project Structure

- `server/` — Python backend (FastAPI)
- `server/cli.py` — CLI entry point (`serve`, `handoff`, `pull`)
- `server/database.py` — SQLite persistence layer
- `server/scheduler.py` — APScheduler-based recurring task runner
- `server/jsonl_parser.py` — Claude Code JSONL session parser
- `server/jsonl_writer.py` — JSONL writer for session export
- `server/routers/` — REST + WebSocket routers (`sessions`, `schedules`, `agents`, `credentials`, `connectors`, `ws`)
- `server/bridges/` — Messaging-platform integrations (`telegram`, base + manager). A chat binds to an agent with a sticky session and a per-chat `verbose` flag (quiet by default → only the agent's natural-language replies, errors and approval prompts reach the chat; `QUIET_SUPPRESSED_EVENTS` hides tool calls/results/cost/status; `/quiet`+`/verbose` toggle it, persisted in `bridge_mappings.verbose`). `/sessions` renders a tappable inline-button picker (`send_session_list`) whose `switch:<id>` callback shares `BridgeManager.switch_session` with the `/switch` command
- `server/agent_manager.py` — Agent CRUD (durable assistant definitions that own sessions/schedules)
- `server/agent_memory.py` — Per-agent native memory (`docs/plans/memory.md`): one canonical markdown dir per agent (`<agents_dir>/<id>/memory/`), shared by both harnesses. Claude points its auto-memory at it via `CLAUDE_COWORK_MEMORY_PATH_OVERRIDE`; Codex via an injected `developer_instructions` blurb naming the dir (its native `features.memories` pipeline is unused — it doesn't run in headless `exec`). Memory is decoupled from both harnesses' config/auth dirs — `CLAUDE_CONFIG_DIR` and `CODEX_HOME` are never touched, so auth and `--resume` transcripts are unaffected. Pure path helpers + idempotent provisioning.
- `server/harness/` — Harness layer: the single boundary for all model/runtime interaction (`docs/plans/harness-layer.md`). One `Harness` class + one `HarnessRun` engine, configured by a `RuntimeProfile` *value* per backend kind (`claude_code`, `codex`) — no per-framework subclasses. Holds `assembly` (shared per-turn MCP/system-prompt assembly), `run` (subprocess+JSONL engine + PATH helpers), `registry` (`get_harness`/`available_backends`), `login` (LoginDriver protocol). Capabilities are derived from the profile; `run_oneshot` powers backend-agnostic `/schedule` parsing.
- `server/connectors/` — Connector framework: `base` (ConnectorBase + backend-neutral MCP entry), `oauth` (provider protocol + redirect-URI login manager), `registry`, built-in `github`/`gmail`, and `custom` (user-defined kinds + generic OAuth provider + `resolve_connector`)
- `server/connector_manager.py` — Connector business logic (install upsert, in-app OAuth-client config DB→env resolve, token-refresh lifecycle, custom-connector CRUD)
- `server/mcp_servers/connectors/` — Per-kind stdio MCP servers (`github`, `gmail`, generic `custom`) + shared token/truncation helpers
- `web/` — React frontend (Vite + TypeScript)
- `tests/` — Backend tests (pytest)
- `web/src/**/*.test.ts` — Frontend unit tests (vitest, colocated with source)
- `web/e2e/` — End-to-end tests (Playwright, auto-cleanup after runs)

## Commands

> **Frontend gotcha**: the backend serves `web/dist/` (the built SPA),
> not `web/src/`. Any source change needs `cd web && bun run build`
> before `octopus serve` / `uvicorn server.main:app` users will see it.
> For live HMR, run `cd web && bun dev` and hit the dev server's port
> (5173) instead of the backend.

```bash
# Backend
.venv/bin/pytest tests/ -v              # run backend tests
.venv/bin/uvicorn server.main:app       # start server (serves web/dist/)

# Frontend
cd web && bun run test                  # run frontend unit tests
cd web && bun run build                 # typecheck + build (refreshes web/dist/)
cd web && bun dev                       # live dev server on :5173

# E2E (Playwright)
cd web && bun run test:e2e              # run e2e tests (headless)
cd web && bun run test:e2e:ui           # run e2e tests with Playwright UI
cd web && npx playwright test --reporter=list  # verbose output
```

## Conventions

- Backend uses Python 3.12+, type hints, async/await
- Frontend uses React 19, TypeScript strict mode, zustand for state
- Use `useSessionStore.getState()` (not hook selectors) inside callbacks/effects that mutate store to avoid re-render loops
- The SDK message parser is patched locally (`.venv/lib/.../message_parser.py`) to handle unknown message types — if you reinstall deps, the patch must be reapplied
- The JS toolchain (`bun`, `node`, `npm`, `npx`) and `codex` live under `~/.nvm/versions/node/*/bin`, **not** on the default PATH. Prepend that bin dir for any frontend/codex command (`export PATH="$HOME/.nvm/versions/node/<ver>/bin:$PATH"`). It's also required for the 4 `test_backend_codex_real.py` tests to resolve `codex` (otherwise they error rather than skip)
