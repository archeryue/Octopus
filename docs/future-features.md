# Roadmap — open work

Octopus has three open initiatives, each with a full tech plan in
[`plans/`](plans/). This file is the **index and the build order**; the
plans are the source of truth. Landed work is removed from here — the
done record is the git log.

Build order is dependency-driven; the rationale and the cross-cutting
coordination points are in the last two sections.

---

## 1. Codex backend — [`plans/codex-backend.md`](plans/codex-backend.md)

**Status**: planned, grounded. **Recommended first.**

A second AI backend (`codex`) beside `claude-code`, driven by the user's
own **ChatGPT subscription** (not an API key). The plan is grounded
against VM0's shipped Codex support and the installed `codex` 0.132.0 —
the `exec --json` event schema, the spawn command, and instruction
injection (`-c developer_instructions`) are verified. One product
decision is still open: the login flow (host `codex login` vs in-app
`--device-auth`, §7/§10).

**Why first**: ready to build, self-contained (no dependency on the
other two), high value, and it nails down the Codex MCP-injection path
that the connectors plan otherwise lists as an open question.

## 2. First-class Agents — [`plans/agent-refactor.md`](plans/agent-refactor.md)

**Status**: planned. **The foundation.**

Promote `Agent` to the durable definition of an assistant (system
prompt, model, credential, MCP set) that **owns** Sessions, Schedules,
and Bridges. Three PRs: schema + backfill → backend + routes → frontend.

**Why second**: it reshapes the ownership graph that Connectors build
on, and it's the keystone for the eventual agent-memory north star. It
touches the same `sessions` table and `_make_backend` as the Codex work
— see coordination notes.

## 3. Connectors — [`plans/connectors.md`](plans/connectors.md)

**Status**: planned. **After Agents.**

First-class third-party **outbound** tools (Notion, **Gmail**, Slack,
GitHub, …) the user installs once and the agent calls as MCP tools.
Notion ships first; **Gmail is Connectors Phase C** — there is no
standalone email feature, email lands here.

**Why last**: Connectors are **agent-scoped** (agent-refactor decisions
#5 / #8), which supersedes the per-session enablement model currently
written in `plans/connectors.md` — that plan must be revised to the
agent-scoped shape before this starts. The Codex MCP path it depends on
is settled by plan #1.

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
