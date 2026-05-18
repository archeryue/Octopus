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

1. **Backend unit tests**: `.venv/bin/pytest tests/ -v` (268 tests)
2. **Frontend unit tests**: `cd web && bun run test` (8 tests)
3. **TypeScript check**: `cd web && npx tsc --noEmit`
4. **E2E tests**: `cd web && bun run test:e2e` (24 tests, Playwright auto-starts servers)

**Zero test failures are acceptable.** All tests must pass before committing. If a test fails, investigate and fix it — do not ignore, skip, or dismiss any failure as "flaky" or "pre-existing".

## Test Coverage

| Suite | Tool | Count | What it covers |
|-------|------|-------|----------------|
| Backend unit | pytest | 268 | Config, models, session manager, REST API (auth, CRUD, 404s, reset), database persistence (incl. credential storage split + refresh-error codes), JSONL parser/writer, CLI (handoff, pull), import API, schedules CRUD + scheduler runner, bridge base/manager/telegram, tunnel config, OAuth provider registry, real-CLI integration (when `claude` is on PATH) |
| Frontend unit | vitest | 8 | Zustand store (token, sessions, messages, status) |
| E2E | Playwright | 31 | Login, session CRUD, real Claude responses (incl. AskUserQuestion + resume), Enter to send, input/state while running, WebSocket reconnect, mobile layout, CLI handoff/pull + roundtrip + API cleanup, Telegram bridge (fake API server), scheduled-tasks UI, waiting-input hint, message queue + Esc interrupt, virtualized chat scrolling, OAuth dialog flow, credential override |

## Project Structure

- `server/` — Python backend (FastAPI)
- `server/cli.py` — CLI entry point (`serve`, `handoff`, `pull`)
- `server/database.py` — SQLite persistence layer
- `server/scheduler.py` — APScheduler-based recurring task runner
- `server/jsonl_parser.py` — Claude Code JSONL session parser
- `server/jsonl_writer.py` — JSONL writer for session export
- `server/routers/` — REST + WebSocket routers (`sessions`, `schedules`, `ws`)
- `server/bridges/` — Messaging-platform integrations (`telegram`, base + manager)
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
