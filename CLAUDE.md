# Octopus Development Rules

## Do It Right The First Time (no MVPs, no future-polish)

**If we choose to do something, we do it perfectly — right now, in
this session.** No "minimal fix", no "MVP for now", no "we'll polish
this later". No deferral of cleanup to a "follow-up" item.

This rule is non-negotiable. Specifically that means:

- Never ship a half-done implementation and document the rest as
  "future work". If the full thing isn't worth doing right now, then
  don't start it at all.
- Never park the cleaner version in a "deferred work" note
  *instead* of doing it. Genuine deferrals (work that needs a real
  second use case, an external dep, or a user decision) belong in
  the relevant plan doc's §10 "What this defers"; nothing else is a
  legitimate place to stash "felt too long".
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

1. **Backend unit tests**: `.venv/bin/pytest tests/ -v` (1208 tests; all of them
   run on a dev box with both CLIs installed and signed in — a skip means a
   lapsed login, not a passing suite). The real-CLI tier is selected by marker,
   not by listing filenames:

   - `pytest -m "not real"` — the hermetic tier: 1174 tests, ~34 s, no CLI
     required. This is what the pre-commit hook and `scripts/check.sh` run.
   - `pytest -m real` — the 34 tests that drive a live model. `real_claude`
     (24) and `real_codex` (8) want a CLI that is installed *and signed in*;
     one two-hop case carries both. `claude_bin` (1) and `codex_bin` (2) want
     only the binary, because a test of `codex login` must not require already
     being logged in.

   The markers resolve in `tests/conftest.py:pytest_runtest_setup`, so the
   availability probe fires at setup and never at import — evaluating it at
   import is what used to make a plain `--collect-only` spawn a real `claude`
   call (16.44 s vs 0.80 s). Run with the nvm bin prepended so `codex`
   resolves (see Conventions).
2. **Frontend unit tests**: `cd web && bun run test` (195 tests)
3. **TypeScript check**: `cd web && npx tsc --noEmit`
4. **E2E tests**: `cd web && bun run test:e2e` (77 tests, no skips, ~4.5 min, Playwright
   auto-starts servers). Split into two buckets for dev iteration —
   `bun run test:e2e:fast` (40 pure-UI tests, ~30 s — login / sessions /
   dialogs / sidebar / virtualized chat / attachments / etc.) and
   `bun run test:e2e:llm` (37 real-LLM tests, ~3 min — chat, /schedule,
   an agent scheduling itself, /showme, /archive, mcp__bg__run, AskUserQuestion, agent-collaboration,
   notifier, codex sign-in, handoff/pull). Anything that drives a real
   `claude` / `codex` turn carries `@llm` in its describe title; the
   `:fast` script uses `--grep-invert @llm`. `codex.spec.ts` needs a
   signed-in `codex` (`codex login --device-auth`); a lapsed login fails
   it rather than hiding it.

**Zero test failures are acceptable.** All tests must pass before committing. If a test fails, investigate and fix it — do not ignore, skip, or dismiss any failure as "flaky" or "pre-existing".

## Test Coverage

| Suite | Tool | Command | Count | Scope |
|-------|------|---------|-------|-------|
| Backend unit | pytest | `pytest -m "not real"` | 1174 | The whole backend, hermetically: config, models, session manager, database + migrations, REST + WS routers, harness layer, connectors, delegations, applications, scheduler, research, token rotation, the in-process MCP namespaces, monitoring. No CLI, no network. ~34 s. |
| Real-CLI tier | pytest | `pytest -m real` | 34 | The cases that must drive a live model: both backends end to end, delegation chains, an agent scheduling itself, memory read-back, fork copy, codex login. Needs a signed-in CLI. |
| Frontend unit | vitest | `cd web && bun run test` | 195 | Zustand store, `useWebSocket`, and every component with logic worth pinning — delegation and sub-agent cards, fork dialog, app icons and backends, streaming buffer, sidebar fold, mobile drawer, viewport height. ~3 s. |
| E2E | Playwright | `cd web && bun run test:e2e` | 77 | The product as a user meets it, in a real browser: login, sessions, real Claude turns, steering, queue + interrupt, mobile layout, connectors, applications, `/rewind`, `/research`, the monitor page. `:fast` (40, ~30 s) skips the `@llm` half; `:llm` (37, ~3 min) is the rest. |

For what any individual test covers, ask the suite rather than this table:
`pytest --collect-only -q`, or `-m real` / `-m "not real"` to see a tier. A
hand-written inventory of 1,208 tests cannot stay true, and it cost ~16 KB of
every session's context to try.

## Project Structure

### Core

- `server/` — Python backend (FastAPI)
- `server/cli.py` — CLI entry point (`serve`, `handoff`, `pull`)
- `server/database.py` — SQLite persistence layer
- `server/scheduler.py` — APScheduler-based recurring task runner
- `server/jsonl_parser.py` — Claude Code JSONL session parser
- `server/jsonl_writer.py` — JSONL writer for session export
- `server/routers/` — REST + WebSocket routers (`sessions`, `schedules`, `agents`, `applications`, `credentials`, `connectors`, `delegations`, `research`, `ws`)
- `server/fork_helpers.py` — Pure helpers for session tree-rewind (`/rewind`): git-anchor capture at turn-start, side-effect classification over parent rows, safe-revert preflight + git-stash execution. Backend-agnostic and side-effect-contained.
- `server/research/` — Native deep research orchestration (`docs/plans/native-deep-research.md`): `ResearchManager` (async job lifecycle, phase pipeline, concurrency cap, cancel + reap), `orchestrator` (scope → search → dedup → verify → synthesize phases), `leaf` (throwaway `HarnessRun` sub-turns for web-search leaves and `run_oneshot` for reasoning leaves), `schemas` (JSON schemas for scope/findings/synthesis). Agent-invoked via `mcp__research__deep_research`; user-invoked via `/research <question>`. Result injected as a follow-up turn; a `ResearchCard` tracks progress in the UI.
- `server/mcp_servers/research.py` — Stdio MCP server exposing `mcp__research__deep_research(question)` to agents; thin HTTP shim to `/api/sessions/{sid}/research`. Returns `research_id` immediately so the model's turn ends cleanly.
- `docs/plans/token-rotation.md` — `OCTOPUS_AUTH_TOKEN` is both the
  credential clients send and the key `crypto.py` derives to encrypt every
  stored secret, so changing it is one server-side operation (`POST
  /api/auth/rotate`): re-key the secrets, rewrite the env files, swap the live
  setting, drop the processes carrying the old one, and hand the new token to
  the clients already holding the old one. No restart, nothing to edit by hand.
- `docs/plans/scheduled-runs.md` — What a scheduled fire is: the overlap
  guard that stops a slow schedule stacking runs, the `[scheduled:…]` marker
  that tells an agent nobody is waiting to answer questions, and
  `last_run_session_id` linking "last run" to the session it happened in.
- `docs/plans/native-subagents.md` — Native sub-agents, surfaced: Claude
  Code's `Task`/`Agent` runs and Codex's `spawn_agent`/`wait` collaboration
  calls normalized onto one `SubagentUpdate`, shown as a live card keyed on
  the spawning tool call's id (never its name — the CLIs rename it), and
  `agents.subagents` → `--agents` so an Octopus agent brings its own helpers.
- `server/app_agent.py` — The conversations a *running* application holds with an agent (`docs/plans/app-agent-access.md`): `/apps/{id}/agent/{agents,conversations,ask,chat}`, mounted under the app's own path so its page reaches them at `agent/…` relative to itself and its backend reaches them with the scoped `OCTOPUS_APP_TOKEN`. A conversation is a real session (`origin='app'`, `sessions.app_id`), so it resumes across turns and can be read in the chat view; the manager registers a turn on the broadcast bus *before* starting it and converts session events into the vocabulary an app consumes (`delta`, `message`, `tool`, `question`, `done`, `error`), streamed as SSE by `chat` or awaited whole by `ask`.
- `server/app_backends.py` — Supervised backend processes for Applications (`docs/plans/application-backends.md`): an app declares a backend with an executable `start.sh` at its root (optionally `install.sh`), Octopus allocates a port, runs them with a minimal environment, waits for the port to accept, proxies `/apps/{id}/api/*` to it, and stops it when idle. Three directories with three owners: `<slug>/` code (rewritten by a rebuild), `<slug>.data/` the app's own state (never touched), `<slug>.runtime/` installed dependencies (deletable to force a clean reinstall).
### Applications

- `server/applications.py` — Applications (`docs/plans/applications.md`): agent-built static web apps. Owns the app directory under `~/.octopus/applications`, the *build session* (a normal session with `origin='application'` whose working dir is the app dir), and a `building|ready|failed` status **derived** from whether the entrypoint exists when a build turn ends — the manager subscribes to the SessionManager broadcast bus (the DelegationManager pattern), so a change typed straight into the build session updates the badge too. `POST /{id}/build` runs another turn in that same session. Archiving
(`POST /{id}/archive` / `/unarchive`) keeps the row **and** the files so the
create page's Archived tab can restore an app exactly as it was; the name
index is live-only, so an archived name frees up (same rule as agents, which
gained `POST /api/agents/{id}/unarchive` for the same tab).
- `server/routers/applications.py` — `/api/applications` CRUD **plus** `/apps/{id}/{path}` — the app itself, streamed out of its directory with traversal + symlink guards, `Cache-Control: no-store`, and bearer / `?token=` / `octopus_app_token`-cookie auth (an iframe can't send an Authorization header)
- `server/agent_manager.py` — Agent CRUD (durable assistant definitions that own sessions/schedules)
- `server/agent_memory.py` — Per-agent native memory (`docs/plans/memory.md`): one canonical markdown dir per agent (`<agents_dir>/<id>/memory/`), shared by both harnesses. Claude points its auto-memory at it via `CLAUDE_COWORK_MEMORY_PATH_OVERRIDE`; Codex via an injected `developer_instructions` blurb naming the dir (its native `features.memories` pipeline is unused — it doesn't run in headless `exec`). Memory is decoupled from both harnesses' config/auth dirs — `CLAUDE_CONFIG_DIR` and `CODEX_HOME` are never touched, so auth and `--resume` transcripts are unaffected. Pure path helpers + idempotent provisioning.
### Agents, memory and delegation

- `server/delegations.py` — Agent-to-agent delegation manager (`docs/plans/agent-collaboration.md`). Subscribes to the SessionManager broadcast bus; on a tracked child session's `assistant_text` / `result` / `error` / `question_request` events, captures + finalises and injects an `[agent-reply:<name> delegation=<id>]` (or `agent-question`, or `agent-error`) follow-up turn into the parent session via the same `start_message` path bg-task delivery uses. Cycle and depth-3 guards walk `parent_session_id`. `answer_pending_question(delegation_id, choice)` drains the child's oldest pending question on the parent's behalf — same Event-signal machinery the human UI uses (first to drain wins). The delegation id IS the child session id; no parallel id space, no new persistence table.
- `server/mcp_servers/ask_agent.py` — Stdio MCP server exposing the four delegation tools to the model: `mcp__ask_agent__ask` / `cancel` / `answer` / `list` (the Python functions are `ask_agent` / `cancel_agent_task` / `answer_agent_question` / `list_agent_tasks`; the `@mcp.tool(name=…)` decorators expose the short forms). `ask(request, name=…, delegation_id=…, files=…)` is bimodal: `name` starts a fresh child session, `delegation_id` continues a prior one in the same child transcript; exactly one id is required. Same `OCTOPUS_API_BASE` / `OCTOPUS_SESSION_ID` env-injection pattern as the bg + ask built-ins; thin HTTP shim to the `/api/sessions/{sid}/delegations` routes, including continuation via `/follow-up`. Added to the default per-agent MCP set; the migration backfills it onto every pre-existing agent row.
- `server/mcp_servers/schedule.py` — Stdio MCP server exposing an agent's own schedules (`docs/plans/schedule-tool.md`): `mcp__schedule__{create,list,update,delete}`. Thin HTTP shim to the session-scoped `/api/sessions/{sid}/schedules` routes, so the agent is derived from `OCTOPUS_SESSION_ID` and can neither see nor touch another agent's schedules. The recurrence is stated outright (`cron` / `interval_seconds` / `run_at`) rather than parsed from English — the caller is already a model, so there is no second one-shot in the loop. In the default per-agent MCP set; the startup backfill adds it to every pre-existing agent.
### The harness boundary

- `server/harness/` — Harness layer: the single boundary for all model/runtime interaction (`docs/plans/harness-layer.md`). One `Harness` class + one `HarnessRun` engine, configured by a `RuntimeProfile` *value* per backend kind (`claude_code`, `codex`) — no per-framework subclasses. Holds `assembly` (shared per-turn MCP/system-prompt assembly), `run` (subprocess+JSONL engine + PATH helpers), `registry` (`get_harness`/`available_backends`), `login` (LoginDriver protocol). Capabilities are derived from the profile; `run_oneshot` powers backend-agnostic `/schedule` parsing.
### Connectors

- `server/connectors/` — Connector framework: `base` (ConnectorBase + backend-neutral MCP entry), `oauth` (provider protocol + redirect-URI login manager), `registry`, built-in `github`/`gmail`, and `custom` (user-defined kinds + generic OAuth provider + `resolve_connector`)
- `server/connector_manager.py` — Connector business logic (install upsert, in-app OAuth-client config DB→env resolve, token-refresh lifecycle, custom-connector CRUD)
- `server/mcp_servers/connectors/` — Per-kind stdio MCP servers (`github`, `gmail`, generic `custom`) + shared token/truncation helpers
- `docs/plans/mobile.md` — Octopus on a phone: the drawer that puts itself
  away, 16px fields (iOS zoom), touch hit areas and hover-only affordances,
  headers that shed context rather than function, and the safe-area insets a
  home-screen install needs. Three separate triggers kept apart: `max-width`
  for layout, `hover: none` for input, `env(safe-area-inset-*)` for hardware.
### Frontend and tests

- `web/` — React frontend (Vite + TypeScript). The interface follows
  `docs/plans/console-redesign.md`: `src/styles/tokens.css` is the whole
  palette + type system (blue `#2563b8`, Hanken Grotesk over IBM Plex Mono for
  anything technical), the sidebar is `SidebarAgents` (two-level: agents with
  their sessions) + `SidebarApplications` + `SidebarManage` (Schedules /
  Connectors / Harness summary rows) + `SidebarAccount`, and every manage or
  create surface is a **main-area page** behind the store's `mainView`
  (`SchedulesPage`, `ConnectorsPage`, `HarnessPage`, `AgentFormPage`,
  `ApplicationFormPage`) rather than a dialog. `PageHeader` is the shared
  breadcrumb bar.
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

### Language and style

- Backend uses Python 3.12+, type hints, async/await
- Frontend uses React 19, TypeScript strict mode, zustand for state
- Use `useSessionStore.getState()` (not hook selectors) inside callbacks/effects that mutate store to avoid re-render loops
### Environment traps

- The SDK message parser is patched locally (`.venv/lib/.../message_parser.py`) to handle unknown message types — if you reinstall deps, the patch must be reapplied
- The JS toolchain (`bun`, `node`, `npm`, `npx`) and `codex` live under `~/.nvm/versions/node/*/bin`, **not** on the default PATH. Prepend that bin dir for any frontend/codex command (`export PATH="$HOME/.nvm/versions/node/<ver>/bin:$PATH"`). It's also required for the 4 `test_backend_codex_real.py` tests to resolve `codex` (otherwise they error rather than skip)
