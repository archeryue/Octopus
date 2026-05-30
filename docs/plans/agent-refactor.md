# Tech Plan: Introduce first-class Agents into Octopus

Reference design: vm0 (`/home/start-up/vm0`), which cleanly splits **Agent (definition) ↔ Session (continuation) ↔ Run (execution)**. Octopus today only has `Session`. We adopt vm0's split *partially* (YAGNI), with a path to the full split later.

## 0. Why this exists, and the shape of this refactor

**The north star is memory.** Sessions are ephemeral — we archive them when a task is done. Octopus today has nowhere to store knowledge, identity, or recurring intent that *outlives* a session. The end goal is an Agent that remembers things across sessions, carries its own configured system prompt and tool/skill set, and can be scheduled to do work regularly.

**This refactor makes the Agent a first-class, durable entity — and gets the ownership architecture fully right in one pass.** Everything that conceptually belongs to "an assistant that persists" moves onto the Agent now: config, schedules, and bridge bindings. It does **not** build memory. Memory is a separate, harder design that hangs off the Agent later (its own table + an MCP `recall`/`remember` tool + system-prompt injection). We build the durable Agent first because memory needs something durable to key off; there is no lock-in risk, since memory only needs an `agent_id`.

**We adopt the reference model, not copy/template.** Sessions hold an FK to their Agent and `_make_backend()` re-reads the Agent's config on every spawn. The justification is precisely memory: future sessions must read what past sessions wrote, so the link has to be live. A consequence we accept on purpose: **editing an Agent's config affects its already-open sessions on their next turn**, not just newly created ones. That is the desired behavior.

**Moving schedules and bridges onto the Agent removes net complexity.** Today `archive_session` runs a `repoint_schedules` / `repoint_bridge_mappings` dance — it has to drag session-owned automation onto the fresh session it creates. Once schedules and bridges point at the *Agent*, archiving a session no longer touches them: both repoint functions and the archive special-case are deleted. Likewise the bridge's `/new` today binds a chat to a bare session id with no durable owner; binding the chat to an Agent (with a sticky session pointer) is the correct shape.

**The mental model: the Agent owns four durable things; the Session is a transient conversation inside that world.** The four are: **config** (identity, system prompt, model, tools), **bridges** (inbound — where humans reach it), **schedules** (autonomous time — when it acts on its own), and **connectors** (outbound — what third-party services it can act on). This refactor builds the first three. Connectors are a future feature (`docs/plans/connectors.md`, not started), but the *ownership decision* is made here: when they land they will be **agent-scoped and kept architecturally separate from bridges** — see §5.8 for why inbound and outbound stay distinct even for the same vendor.

**Decisions baked in** (see §9 for the full list):

- Backfill creates **one** Default Agent, not one-per-credential.
- **No** `system_prompt_override` on sessions.
- `working_dir` stays **session-only**; Agents are not path-aware.
- Tool policy is **two newline-separated TEXT columns** (`tool_allow` / `tool_deny`), not a freeform JSON blob.
- Per-agent MCP set and per-session connectors are reconciled as a **union** (§5.5).
- `agent_id` is enforced **in the API layer**, left nullable in SQLite (no `SET NOT NULL` dance).
- **Schedules and bridges move onto the Agent in this refactor** (not deferred). Firing model and routing are resolved in §9.

## 1. Goals

- Introduce an `Agent` as the **durable definition** of an AI assistant: name, system prompt, model, tool/MCP allowlist, attached credential — plus the automation it owns: **schedules and bridge bindings**.
- Demote `Session` to a **conversation thread** owned by one Agent. A session is *an instance of talking to an agent*, not the place where the agent is configured. Sessions are disposable; the Agent is not.
- Leave the ownership graph correct and final after this lands: Agent → (Sessions, Schedules, Bridges). No follow-up re-parenting step.
- Preserve existing single-user, single-port FastAPI + SQLite shape. No new infra.

## 2. Non-goals (for this refactor)

- **No memory store.** It's the reason the Agent exists, but it's designed and built separately, after this lands. The durable Agent here is its foundation.
- **No connectors built here.** Outbound third-party tools (`docs/plans/connectors.md`) are a separate future feature. This refactor only records the ownership decision (agent-scoped, separate from bridges — §5.8) so the connectors plan slots in without a schema redo.
- No multi-tenant / org model. Octopus stays single-user.
- No content-addressed agent versioning (vm0's `agentComposeVersions`). Track agent edits in-place; revisit later.
- No separate `Run` table. A "send_message turn" stays implicit inside a session; lift it only if/when we need per-turn billing, snapshots, or A2A triggers.
- ~~No multi-agent orchestration (A2A). Single agent per session.~~ —
  **superseded** by [`agent-collaboration.md`](agent-collaboration.md): A2A
  shipped as the `mcp__ask_agent__*` MCP tools without needing a Run
  table. A delegation is a normal `Session` row with `parent_session_id`
  set + `origin='delegation'`; the trigger is the MCP tool call. The
  Run-table prerequisite suggested in §9 below turned out to be wrong —
  we got there without one. A delegation child is still a single-agent
  session; the multi-agent shape is built from the chain of sessions.

## 3. Reference mapping vm0 → Octopus

| vm0 | Octopus today | Octopus after this refactor |
|---|---|---|
| `zero_agents` (definition) | *missing* | **new** `agents` table |
| `agentComposeVersions` (snapshots) | *missing* | *skipped* |
| `agentSessions` (conversation) | `sessions` table (mostly) | `sessions` (FK → agent) |
| `agentRuns` (per-execution) | implicit in `_run_backend()` per turn | *stays implicit* |
| `zeroAgentSchedules` | `schedules` (FK → session) | `schedules` (FK → **agent**) |
| `permissionPolicies` (firewall) | none | `agents.tool_allow` / `agents.tool_deny` (newline-separated tool + MCP names) |
| `customSkills` | none | `agents.mcp_servers` (curated built-in MCP set per agent) |
| `modelProviderId` / `selectedModel` | `sessions.credential_id` only | `agents.credential_id` + `agents.model` |
| bridge mappings | `bridge_mappings` (FK → session) | `bridge_mappings` (FK → **agent**, + sticky nullable `session_id`) |
| (no analogue — Octopus-specific) | none | *future* `agent_memory` (the north star, **not this refactor**) |

## 4. Data model changes

### 4.1 New table `agents`

```sql
CREATE TABLE agents (
  id TEXT PRIMARY KEY,                    -- 12-char hex, same scheme as sessions
  name TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  avatar TEXT,                            -- emoji or URL, optional
  system_prompt TEXT NOT NULL DEFAULT '',
  model TEXT,                             -- e.g. "claude-opus-4-7"; null = backend default
  credential_id TEXT REFERENCES backend_credentials(id) ON DELETE SET NULL,
  mcp_servers TEXT NOT NULL DEFAULT '["ask","bg","viewer"]',
                                          -- JSON array of *built-in* Octopus MCP server ids.
                                          -- Opaque to SQL: read whole, parsed in Python.
  tool_allow TEXT NOT NULL DEFAULT '',    -- newline-separated tool/MCP names; empty = allow all
  tool_deny  TEXT NOT NULL DEFAULT '',    -- newline-separated; deny takes precedence over allow
  is_system INTEGER NOT NULL DEFAULT 0,   -- 1 = the protected Default Agent (cannot be deleted)
  archived INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX agents_name_unique ON agents(name) WHERE archived = 0;
```

**On the list-shaped columns.** None of these are ever queried *inside* with `json_extract` — we always load the whole agent row and parse in Python. So the storage format is pure serialization. `mcp_servers` is a small closed set of identifiers, kept as a JSON array (matches existing Octopus convention). `tool_allow` / `tool_deny` are open-ended name lists kept as newline-separated TEXT — two columns, so "allow" and "deny" are first-class and readable, never a nested `{"allow":[...],"deny":[...]}` blob. Empty `tool_allow` means "no allowlist restriction"; `tool_deny` always wins on conflict.

### 4.2 `sessions` changes

```sql
ALTER TABLE sessions ADD COLUMN agent_id TEXT REFERENCES agents(id) ON DELETE CASCADE;
ALTER TABLE sessions ADD COLUMN origin TEXT NOT NULL DEFAULT 'user';  -- 'user' | 'schedule' | 'bridge'
-- NO system_prompt_override column (cut: agent config must not leak into the session).
-- credential_id: KEPT as an optional per-session override during this refactor.
-- working_dir:   UNCHANGED — session-only. Agents are not path-aware.
```

`agent_id` is **nullable in the SQLite schema** but **required by the API**: `create_session` refuses a missing `agent_id`, and the migration backfills every existing row, so no live row stays null. We do not attempt `ALTER TABLE ... SET NOT NULL` (SQLite has no such statement; it would force a table rebuild). Enforcing in the API layer is simpler and sufficient for a single-user app.

`origin` records who created the session, so the UI can group it and lifecycle policy can act on it (scheduled fires auto-archive on idle; see §5.6). Existing rows backfill to `'user'`.

`ON DELETE CASCADE` is a backstop, not the primary path: `delete_agent` is API-guarded to refuse when sessions exist (§5.1), so the cascade rarely fires.

### 4.3 `schedules` → owned by the Agent

```sql
ALTER TABLE schedules ADD COLUMN agent_id TEXT REFERENCES agents(id) ON DELETE CASCADE;
-- backfill agent_id from the schedule's current session (see §4.5), then:
ALTER TABLE schedules DROP COLUMN session_id;   -- SQLite ≥ 3.35 (2021) supports DROP COLUMN
```

A schedule belongs to the Agent ("every morning, summarize my inbox"), not to a throwaway thread. There is no persistent `session_id` on a schedule anymore — each fire materializes its own session under the Agent (§5.6).

### 4.4 `bridge_mappings` → bound to the Agent, with a sticky session

```sql
ALTER TABLE bridge_mappings ADD COLUMN agent_id TEXT REFERENCES agents(id) ON DELETE CASCADE;
-- backfill agent_id from the mapped session (see §4.5), then relax session_id:
--   session_id becomes NULLABLE and means "the currently-active thread for this chat".
```

`(platform, chat_id)` now binds durably to an **Agent**. `session_id` is demoted to a *sticky pointer* at the currently-open conversation, which rolls as sessions come and go (§5.5). A chat that has never opened a session has `session_id = NULL`.

> SQLite can't change a column's NOT NULL in place. `bridge_mappings` is small, so this column relaxation is done with the standard create-new-table / copy / drop / rename move inside `_apply_migrations` (guarded so it only runs while the old shape is present). `schedules.session_id` is removed the same way if the runtime SQLite predates 3.35.

### 4.5 Migration & backfill

Stick with Octopus's existing additive-SQL migration style (`server/database.py:_apply_migrations`, try/except per statement). No Alembic. Order matters — Agents and `sessions.agent_id` must be populated before schedule/bridge backfill, since those derive their `agent_id` through the session.

**Backfill** (one-time, idempotent, runs on every boot):

1. If no row with `is_system = 1` exists, create the **Default Agent**: `is_system=1`, name `"Default"`, empty system prompt, no model override, `mcp_servers` = `["ask","bg","viewer"]`.
2. `UPDATE sessions SET agent_id = <default id> WHERE agent_id IS NULL`. (`origin` defaults to `'user'`.)
3. `UPDATE schedules SET agent_id = (SELECT s.agent_id FROM sessions s WHERE s.id = schedules.session_id) WHERE agent_id IS NULL`. Then drop `schedules.session_id`.
4. `UPDATE bridge_mappings SET agent_id = (SELECT s.agent_id FROM sessions s WHERE s.id = bridge_mappings.session_id) WHERE agent_id IS NULL`. Existing `session_id` stays as the initial sticky pointer.

"I never used agents" maps to "I have exactly one agent," and all my schedules/bridges keep working because they inherit that agent. No per-credential agents — those are created by the user when they want them. Existing `sessions.credential_id` values are untouched and continue as per-session overrides.

**Idempotency:** a second boot finds the system agent present, zero null `agent_id` rows across all three tables, and the column-shape migrations already applied, so every step no-ops.

## 5. Backend changes

### 5.1 New module: `server/agent_manager.py`

Mirrors `session_manager.py`'s shape but is **stateless** (agents are pure DB rows — no in-memory subprocess). Responsibilities:

- `create_agent(name, system_prompt=..., model=..., credential_id=..., mcp_servers=..., tool_allow=..., tool_deny=...)`
- `update_agent(agent_id, **fields)` — partial update; bumps `updated_at`
- `list_agents(include_archived=False)` — each row carries a count of active (non-archived) sessions
- `get_agent(agent_id)` — hydrated agent + active-session count
- `archive_agent(agent_id)` — soft delete; cascade-archives the agent's sessions. Refuses the `is_system` agent.
- `delete_agent(agent_id)` — hard delete, only if the agent has zero sessions **and** is not `is_system`. (Cascades to its schedules/bridges via FK; guarded so this is rare.)

### 5.2 `SessionManager` changes (`server/session_manager.py`)

- `create_session(name, working_dir, credential_id)` becomes `create_session(agent_id, name=None, working_dir=None, origin="user")`:
  - Refuse creation if `agent_id` is missing or unknown.
  - `working_dir` defaults to `settings.default_working_dir` (not an agent field — Agents aren't path-aware).
  - `name` defaults to a generated label (`"{agent.name} — {timestamp}"`).
- `_make_backend(session)` reads config from the session's Agent at spawn time (this is the live-reference point):
  - `agent.system_prompt` → backend system prompt
  - `agent.model` → backend model arg
  - credential resolution: **`session.credential_id` if set, else `agent.credential_id`** (session override wins during this refactor)
  - `agent.mcp_servers` → which built-in MCP servers to register (§5.7)
  - `agent.tool_allow` / `agent.tool_deny` → translate to the backend's `allowed_tools` / `disallowed_tools`
- `Session` dataclass gains `agent_id: str`, `origin: str`, and a lazy `agent` accessor that pulls the row through `AgentManager`.
- **`archive_session` simplifies.** Delete the `repoint_schedules` / `repoint_bridge_mappings` calls (and the DB functions): schedules/bridges no longer point at the session, so there's nothing to repoint. The only bridge-aware step that remains: if the archived session is some chat's sticky pointer, null that pointer so the next inbound message opens a fresh thread (`UPDATE bridge_mappings SET session_id = NULL WHERE session_id = ?`).

### 5.3 `ScheduleRunner` changes (`server/scheduler.py`)

The job now carries `agent_id`, not `session_id`. `_fire` materializes a fresh session per fire:

```
async def _fire(schedule_id, agent_id, prompt):
    session = await session_mgr.create_session(agent_id, origin="schedule")
    async for _ in session_mgr.send_message(session.id, prompt): pass
    await db.update_schedule(schedule_id, last_run_at=now)
    # session auto-archives on idle (§5.6)
```

Continuity across fires comes from agent memory (later), not a reused session — consistent with "sessions aren't long-standing." Each fired session is flagged `origin='schedule'`.

### 5.4 Routes

New `server/routers/agents.py`:

| Method | Path | Purpose |
|---|---|---|
| `GET`    | `/api/agents` | list (with active session counts) |
| `POST`   | `/api/agents` | create |
| `GET`    | `/api/agents/{id}` | fetch |
| `PATCH`  | `/api/agents/{id}` | update (any subset of fields) |
| `POST`   | `/api/agents/{id}/archive` | archive (cascade-archives sessions; refused for `is_system`) |
| `DELETE` | `/api/agents/{id}` | delete (only if no sessions and not `is_system`) |
| `GET`    | `/api/agents/{id}/sessions` | sessions under this agent |
| `POST`   | `/api/agents/{id}/sessions` | create a new session under this agent (preferred path) |
| `GET`/`POST` | `/api/agents/{id}/schedules` | list / create schedules for this agent |

Schedule CRUD moves to the agent scope. `PATCH`/`DELETE` on individual schedules stay at `/api/schedules/{id}`. Existing `POST /api/sessions` keeps working but **requires `agent_id`** (defaults to the Default Agent when omitted, for exactly one release, then that fallback is removed). The old session-scoped schedule route becomes a thin compatibility wrapper that resolves the session's agent for one release.

### 5.5 `BridgeManager` changes (`server/bridges/manager.py`)

The in-memory map becomes `"platform:chat_id" -> (agent_id, session_id|None)`. Message routing:

1. Resolve the mapping. An unbound chat (first contact) binds to the **Default Agent** with `session_id = NULL`.
2. If `session_id` is `NULL` or its session is archived/gone → `create_session(agent_id, origin="bridge")`, store it as the new sticky pointer.
3. Route the message to that session.

Commands:
- `/new [name]` → force a fresh session under the bound agent; update the sticky pointer.
- `/agent <name|id>` → rebind this chat to a different agent; clears the sticky session.
- `/switch <session_id>` → point the sticky pointer at an existing session (must belong to the bound agent).
- `/sessions`, `/current`, `/help` → unchanged in spirit, scoped to the bound agent.

This deletes the "no session connected" dead-end: a bound chat always has an agent, and a thread is created on demand.

### 5.6 Session lifecycle for non-`user` origins

A session with `origin='schedule'` **auto-archives when it next goes idle** (hook into the existing idle path that fires the session-idle notifier in `_drive_messages`). This bounds the active-session list under heavy schedules. `origin='bridge'` sessions persist (they're an ongoing conversation) until the user or `/new` rolls them.

> Archived scheduler sessions still accumulate in the DB (one per fire). A retention sweep — "prune archived `origin='schedule'` sessions older than N days" — is a small follow-up knob, not a blocker, and is called out here so it isn't a hidden gap.

### 5.7 Pydantic contracts & MCP set

- `server/models.py`: add `AgentRead`, `AgentCreate`, `AgentUpdate`; add `agent_id: str` and `origin: str` to `SessionRead` (and `agent_id` to `SessionCreate`). Move schedule contracts to carry `agent_id`. Regenerate `web/src/api/contracts.ts` from `openapi.json` (existing flow).
- **MCP servers: agent's built-in set ∪ agent's connectors.** The backend builder registers the **built-in** servers in `agent.mcp_servers` (default `["ask","bg","viewer"]` for the Default Agent, so behavior is unchanged). The effective MCP set for a turn is `agent.mcp_servers` (built-in) **∪** the agent's enabled connectors (outbound third-party servers, when that feature lands — §5.8). Both layers are **agent-scoped**, so a scheduled or bridge-spawned session inherits them automatically; there is no per-session connector enablement in v1. This supersedes the per-session model sketched in `docs/plans/connectors.md` (see §5.8).

### 5.8 Bridges vs Connectors — the inbound/outbound boundary

Octopus has two third-party-integration surfaces, and they must stay **separate concepts**, not be merged into one "integration" object — even when they involve the same vendor (e.g. Slack). The distinction is **direction**:

| | **Bridge** (inbound) | **Connector** (outbound) |
|---|---|---|
| Direction | external → agent | agent → external |
| Initiator | a human messages the agent | the agent calls a tool mid-turn |
| What it is | a transport / front door | a capability / the agent's hands |
| Driven by | webhook / push | permission / pull |
| Auth | bot/app token + webhook secret | personal OAuth token, scoped (e.g. `chat:write`) |
| Example | "talk to my agent from Telegram/Slack" | "agent reads my Gmail / posts to a Slack channel" |

**This matches vm0, which keeps them strictly separate.** vm0's outbound `connectors` table binds to the agent via `user_connectors (agentId → zero_agents.id)`; its inbound channels are entirely separate tables (`slack_org_installations`, `slack_org_thread_sessions`, `telegram_installations`, …) with their own webhook + routing layer. A "slack" connector (outbound) and a `slack_org_installation` (inbound) are different DB entities with **no FK between them** — "Slack" names two integration patterns, not one. (vm0 also records the inbound source as `Run.triggerSource`, which is exactly our `sessions.origin`.)

**Why not unify (the tempting "let the Slack connector also receive messages" idea):** the inbound half needs a bot app + webhook endpoint + event parsing + identity linking + thread→session routing; the outbound half needs a user OAuth token exposed as an MCP tool. They're different auth grants with different scopes and lifecycles. Merging them wouldn't save that work — it would hide two unrelated halves inside one confusing object. Keeping them separate is also what makes the natural case expressible: an agent **reachable in Slack** (bridge) that **also posts to other Slack channels** (connector), as two records, on purpose.

**What we *do* consolidate** (the legitimate anti-silo concern):

1. **One inbound framework.** `server/bridges/` is the single inbound abstraction; Slack-inbound, when added, is a new `Bridge` subclass next to Telegram — not a parallel subsystem.
2. **One credential vault.** Bridges (bot tokens) and connectors (OAuth tokens) reuse the same encrypted storage + `needs_reconnect` flow (`backend_credentials` / `credential_secrets`). That shared plumbing is the *only* thing they share.
3. **Both bind to the Agent.** Bridge binding → agent (§4.4, §5.5); connector grant → agent (future, §5.7). Same ownership tier, opposite directions.

## 6. Frontend changes (`web/src/`)

- **Zustand store**: introduce `agentStore` (agent list, active agent, CRUD) alongside the existing `sessionStore` (now filtered by active agent). Keep the split minimal — `sessionStore` gains an `activeAgentId` and filtered selectors rather than duplicating agent state.
- **Sidebar (`SessionList.tsx`)** becomes two-pane: top = agent list (avatar / name, click to select); bottom = sessions under the selected agent + "new session" button. Scheduler-origin sessions can be visually grouped or dimmed via `origin`.
- **New `AgentSettings.tsx`** — form for system prompt, model, credential, MCP allowlist, tool allow/deny. Reachable from a gear icon on each agent row. The Default Agent's delete action is disabled (it's `is_system`).
- **`ChatView`** unchanged structurally; show the agent name/avatar in the header so the user knows which agent they're talking to.
- **`ScheduleList`** moves to **agent scope** — schedules are configured on the agent (a tab in `AgentSettings` or a section under the selected agent), not on a session.

## 7. Phased rollout

Three PRs. Memory and the `credential_id` cleanup are the only things explicitly *later*; the ownership graph is final after P3.

| Phase | Scope | Ships independently? |
|---|---|---|
| **P1 — Schema & backfill** | `agents` table; `sessions.agent_id` + `origin`; `schedules.agent_id` (drop `session_id`); `bridge_mappings.agent_id` (+ nullable `session_id`); idempotent migration + Default Agent backfill + derive agent_id for schedules/bridges. No API/UI yet. | yes — invisible to user |
| **P2 — Backend services & routes** | `AgentManager`, `/api/agents` (+ agent-scoped schedules) routes; `_make_backend()` reads from agent; `ScheduleRunner` fires per-agent into fresh sessions; `BridgeManager` binds chats to agents with sticky sessions; `archive_session` simplified, repoint functions deleted. `POST /api/sessions` requires `agent_id` (Default-Agent fallback for one release). | yes |
| **P3 — Frontend** | Agent sidebar, agent settings modal (incl. schedules), session-create flow through agent, agent header in chat. | yes |

P2 needs P1; P3 needs P2.

**Later (separate, not part of this refactor):**

- **Cleanup**: drop the `agent_id`-optional fallback in `POST /api/sessions`; drop the legacy session-scoped schedule wrapper; decide the fate of `sessions.credential_id`.
- **Retention sweep** for archived `origin='schedule'` sessions (§5.6).
- **Memory** — the north star: `agent_memory` store + `recall`/`remember` MCP tool + system-prompt injection. Designed on its own.

## 8. Tests (integration, not unit)

- New integration tests under `tests/`:
  - `test_agents_api.py` — full CRUD + name-uniqueness + `is_system` delete/archive protection + cascade-archive of sessions.
  - Extend `test_sessions_api.py` — session creation requires `agent_id`; inherits agent's system prompt / model / credential; `session.credential_id` overrides `agent.credential_id`.
  - `test_backend_args.py` — `_make_backend()` consults the agent for system prompt / model / MCP set / tool allow-deny.
  - `test_scheduler.py` (extend) — a fire creates a fresh `origin='schedule'` session under the agent, runs the prompt, and the session auto-archives on idle.
  - `test_bridge_manager.py` (extend) — unbound chat binds to Default Agent; inbound message opens a sticky session; `/new` rolls it; `/agent` rebinds; archiving the sticky session nulls the pointer.
  - `test_migration_backfill.py` — boot a DB with the old schema (sessions + session-owned schedules + bridge mappings), run `_apply_migrations()` **twice**, assert: one `is_system` agent, every session/schedule/bridge backfilled to it, `schedules.session_id` gone, and the second run no-ops (idempotency).

- **E2E (Playwright, `web/e2e/`)** — three paths:
  1. **Happy path:** create agent → create session under it → message streams back.
  2. **Live-reference (load-bearing):** edit an agent's system prompt, then send a turn in an *already-open* session under that agent and assert the new prompt takes effect. This is the behavior the whole reference model is chosen for; it must be proven, not assumed.
  3. **Agent-owned schedule:** create a schedule on an agent, let it fire (short interval), assert a fresh scheduler-origin session ran and then archived.

## 9. Decisions — resolved

1. **`working_dir`**: session-only. Agents are not path-aware. Bridge/scheduled sessions fall back to `settings.default_working_dir`.
2. **`sessions.credential_id`**: kept as a per-session override; `_make_backend` uses it ahead of `agent.credential_id`. Its fate is decided in the later cleanup step.
3. **Config propagation**: live reference — editing an Agent affects its open sessions on their next turn. Accepted as desirable; proven by the §8 live-reference e2e.
4. **Default Agent immortality**: an explicit `is_system` flag, not a "cannot delete last agent" rule. `archive_agent`/`delete_agent` refuse it.
5. **MCP set**: union — `agent.mcp_servers` (built-in) ∪ the agent's enabled connectors (third-party, future). Both **agent-scoped**, so spawned sessions inherit them; no per-session connector enablement in v1. This supersedes the per-session model in `docs/plans/connectors.md`.
6. **Schedule firing model**: **fresh session per fire**, flagged `origin='schedule'`, **auto-archived on idle**. No long-lived scratch session; continuity is agent memory's job (later). A retention sweep for old archived scheduler sessions is a follow-up knob.
7. **Bridge ownership/routing**: a chat binds durably to an **Agent** (Default Agent on first contact). Inbound messages route to a **sticky session** pointer, creating a fresh `origin='bridge'` session when none/archived. `/new` rolls the thread; `/agent` rebinds the chat; `/switch` repoints within the agent.
8. **Bridges vs connectors stay separate** (§5.8): inbound (bridge) and outbound (connector) are distinct concepts even for the same vendor, matching vm0. They share only the credential vault; both are agent-scoped. Not merged into one "integration" object.

## 10. What we are *not* doing (and why)

- **No memory in this refactor.** It is the goal, but it's a harder design (storage shape, injection strategy, growth/eviction) that deserves its own spec. The durable Agent built here — owning config, schedules, and bridges — is its foundation.
- **No `Run` table.** Per-turn execution is well-contained in `SessionManager`; a Run table would force every turn into a DB lifecycle. Add it when we actually need per-turn billing, A2A triggers (turned out we *didn't* need it — A2A shipped via [`agent-collaboration.md`](agent-collaboration.md) without a Run table), or persistent execution logs separate from messages.
- **No compose versioning.** Editing an agent changes its row. vm0's "snapshot at run time" guarantee is valuable for reproducibility but irrelevant until Octopus has scheduled fleets or external triggers that race with edits.
- **No firewall/proxy.** Octopus runs `claude` as a local subprocess on the user's machine. There is no untrusted egress to police. vm0's mitmproxy story doesn't translate.
- **No org/visibility model.** Single user.
