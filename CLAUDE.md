# Octopus Development Rules

## After Every Code Change

You MUST verify your changes before considering them done:

1. **Backend unit tests**: `.venv/bin/pytest tests/ -v` (95 tests)
2. **Frontend unit tests**: `cd web && bun run test` (8 tests)
3. **TypeScript check**: `cd web && npx tsc --noEmit`
4. **E2E tests**: `cd web && bun run test:e2e` (12 tests, Playwright auto-starts servers)

All tests must pass before committing.

## Test Coverage

| Suite | Tool | Count | What it covers |
|-------|------|-------|----------------|
| Backend unit | pytest | 95 | Config, models, session manager, REST API (auth, CRUD, 404s), database persistence, JSONL parser/writer, CLI (handoff, pull), import API |
| Frontend unit | vitest | 8 | Zustand store (token, sessions, messages, status) |
| E2E | Playwright | 12 | Login flow, session create/delete, sending messages to Claude with real responses, Enter key send, input disabled while running, WebSocket connection, mobile responsive layout |

## Project Structure

- `server/` — Python backend (FastAPI)
- `server/cli.py` — CLI entry point (`serve`, `handoff`, `pull`)
- `server/database.py` — SQLite persistence layer
- `server/jsonl_parser.py` — Claude Code JSONL session parser
- `server/jsonl_writer.py` — JSONL writer for session export
- `web/` — React frontend (Vite + TypeScript)
- `tests/` — Backend tests (pytest)
- `web/src/**/*.test.ts` — Frontend unit tests (vitest, colocated with source)
- `web/e2e/` — End-to-end tests (Playwright, auto-cleanup after runs)

## Commands

```bash
# Backend
.venv/bin/pytest tests/ -v              # run backend tests
.venv/bin/uvicorn server.main:app       # start server

# Frontend
cd web && bun run test                  # run frontend unit tests
cd web && bun run build                 # typecheck + build
cd web && bun dev                       # start dev server

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
