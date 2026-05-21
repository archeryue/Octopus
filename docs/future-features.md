# Roadmap — open work

Octopus has three initiatives, each with a full tech plan in
[`plans/`](plans/). This file is the **index**; the plans are the source
of truth.

**Progress (2026-05-19 session):** #2 Agents has **landed** (all suites
green); #1 Codex backend is **built to its verifiable bound** (data model,
dispatch, `codex.py`, normalizer + `build_args` tests, `GET /api/backends`,
backend selector UI) with only the subscription-gated parts deferred; #3
Connectors' plan has been **revised to agent-scoped** and awaits
implementation. Details per-initiative below.

**Update (2026-05-20):** Connectors **re-scoped to Gmail + GitHub first**;
**Notion dropped** — the user migrated their documents to Obsidian (local
Markdown the agent already reads/writes directly via the filesystem, so no
connector is needed).

---

## 1. Codex backend — [`plans/codex-backend.md`](plans/codex-backend.md)

**Status**: ✅ **DONE & live-verified (2026-05-19).** Only the *optional*
in-app login UI is deferred (host `codex login` works today).

A second AI backend (`codex`) beside `claude-code`, driven by the user's own
**ChatGPT subscription**. **Shipped:** `sessions.backend` column + migration;
`_make_backend` dispatch + `wants_premature_exit_recovery` opt-out;
`server/backends/codex.py` (`exec --json` normalizer, `build_args` grounded on
real `codex` 0.132.0, MCP injection via per-session `-c mcp_servers.*`
overrides, **stdin closed on spawn** so codex doesn't block); `GET
/api/backends`; credential↔backend validation; nvm-aware binary discovery;
the Claude/Codex selector in the new-session form.

**Phase C (live, on a logged-in subscription) — confirmed end-to-end:** the
event schema matches (text / command_execution / **mcp_tool_call** — the last
was a real normalizer gap caught and fixed); MCP injection is honored (codex
launched our viewer server, env passed through, model called the tool); resume
works. Verified by `tests/test_backend_codex_real.py` (4/4, gated on a login)
and `web/e2e/codex.spec.ts` (full UI → real response). Schema recorded in
`docs/codex-protocol-notes.md`.

**Deferred (product decision, not blocked):** the in-app `--device-auth` login
UI (§6.3 / §10). Host `codex login` already works — no API key, no UI needed.

## 2. First-class Agents — [`plans/agent-refactor.md`](plans/agent-refactor.md)

**Status**: ✅ **LANDED (2026-05-19).** The foundation.

Promoted `Agent` to the durable definition of an assistant (system prompt,
model, credential, MCP set, tool policy) that **owns** Sessions, Schedules,
and Bridges. Shipped all three phases: schema + idempotent backfill (Default
Agent); `AgentManager` + `/api/agents` routes; `_make_backend` reads config
from the agent each turn (live-reference); scheduler fires per-agent into
fresh auto-archiving sessions; bridges bind chats to agents with a sticky
session; frontend two-pane sidebar + Agent settings dialog + agent-scoped
schedules. All suites green (434→450 backend, 33 vitest, 50/50 e2e).

The agent-memory north star (Deferred, below) now has its durable key.

## 3. Connectors — [`plans/connectors.md`](plans/connectors.md)

**Status**: planned; **plan revised to agent-scoped** and **re-scoped to
Gmail + GitHub first** (see the two banners atop `connectors.md`). **After
OAuth client registration.**

First-class third-party **outbound** tools the user installs once and the
agent calls as MCP tools. **v1 ships Gmail + GitHub** — the two services the
user lives in daily:
- **Gmail** — search / read / label / draft, plus send gated behind an
  explicit per-turn confirm. There is no standalone email feature; email
  lands here.
- **GitHub** — issues / PRs / repo + file reads / code search /
  create + comment.

**Notion is dropped** (2026-05-20): the user moved their documents to
Obsidian, i.e. local Markdown the agent already edits directly through the
filesystem — no connector required. The Notion-specific design in
`connectors.md` (§6.1, the Phase-B "first connector", the system-prompt
example) is superseded; **GitHub takes the "second proof connector" slot**,
and its concrete OAuth + tool surface gets pinned down at implementation and
verified live (tracked in `connectors.md` §13 "still unverified"), rather
than fabricated now.

**Why still pending**:
- The plan must be applied in its **agent-scoped** shape (agent-refactor
  decisions #5 / #8) — connectors enable per *agent*, not per session, which
  supersedes the per-session model still written in the plan body.
- Needs **OAuth client registration** with Google (Gmail) and GitHub before
  it can be verified end-to-end; the §8 fake-HTTP fixtures let Phases A–B be
  built and tested without live clients.
- The Codex MCP injection path it depends on is settled by plan #1.

---

## Cross-plan coordination

- **`sessions` table**: Codex adds `backend`; Agents add `agent_id` +
  `origin`. All additive `ALTER`s in `database.py:_apply_migrations` —
  no conflict, but whichever lands second should expect the other's
  columns.
- **`_make_backend`** (`session_manager.py:968`): Codex adds dispatch on
  `session.backend`; Agents make it pull config (prompt / model / MCP /
  credential) from the agent. They compose — the second one layers onto
  the first rather than replacing it.
- **Connectors ↔ Agents**: `connectors.md`'s per-session enablement is
  replaced by agent-scoping. Revise that plan when Connectors begins
  (the supersession is already recorded in `agent-refactor.md` §5.5 /
  decision #5).
- **Connectors ↔ Codex**: the "verify `codex` TOML MCP config" open
  question (`connectors.md` §10.1) is answered by `codex-backend.md`
  §5.3 once plan #1 ships.

## Deferred (no plan yet)

- **Agent memory** — the north star behind the Agent refactor
  (`agent_memory` store + `recall` / `remember` MCP tool + system-prompt
  injection). Explicitly designed on its own, after Agents lands.
