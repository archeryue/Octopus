# `octo` — one way in to Octopus's data

> **Status:** in-progress — Design: a single client, `octo`, over one server-side interface layer, so agents, Applications and people reach Octopus's data the same way instead of each hacking the web API or the database.

> Every feature planned next — the Ledger and its Board, the Ontology, the Router
> ([`agent-router-and-channels.md`](agent-router-and-channels.md)) — needs the same
> internal data, read by agents, by Applications and by people. Today each
> consumer gets there its own way. This plan puts one interface on the server
> and one client, `octo`, in front of it. Nothing is built yet.

---

## 1. The problem: every consumer finds its own way in

| Consumer | How it reaches Octopus today |
|---|---|
| An agent in a session | One hand-written MCP namespace per capability — `bg`, `ask`, `ask_agent`, `research`, `schedule`, plus the connector namespaces — each its own thin HTTP shim ([`polish-2026-09.md`](polish-2026-09.md) §4 B1) |
| An Application | Only the agent API under its own `/apps/<id>/agent/…`, with `OCTOPUS_APP_TOKEN` ([`app-agent-access.md`](app-agent-access.md)); no data API at all |
| An operator, or an agent checking the system | Straight to SQLite. `octopus monitor` reads `octopus-metrics.db` directly; the nightly SRE check and ad-hoc investigations open `octopus.db` read-only |

Each new feature would add one more path. A path that reads the database
bypasses the one place that scopes data to its owner — the multi-tenancy
boundary ([`multi-tenancy.md`](multi-tenancy.md)) — and every web-API caller
re-learns routes that were written for the SPA.

And the next features multiply the consumers. Board and Ontology are to be
**official Applications users can change freely** (§6), so their data has to
be reachable from an Application through a stable, permissioned surface. That
data still belongs to Octopus and is served by Octopus.

---

## 2. Design

```
   agent (Bash)   Application backend   person (terminal)   MCP tools   the SPA
        │                 │                    │                │          │
        └──────── octo ───┴────────────────────┘                │          │
                    │ HTTP, bearer = who is calling             │          │
   ┌────────────────▼───────────────────────────────────────────▼──────────▼───┐
   │  /api/v1 — the interface layer: one route per operation, typed, scoped     │
   │  permission check · tenant scoping · audit — done here, once               │
   └────────────────────────────────┬──────────────────────────────────────────┘
                                    ▼
                      managers (sessions, ledger, ontology, …) → DB
```

### 2.1 The interface layer is the single source of truth

- A versioned, typed surface (`/api/v1/…`) for the operations agents and
  Applications need. Same FastAPI + pydantic stack as today, so it is in the
  OpenAPI schema and in the generated `contracts.ts`.
- **Every check happens here**: who the caller is, what they may do, which
  account's rows they see. A client cannot widen it.
- **`octo` is a thin client over it. It never opens a database.** The MCP
  namespaces become thin shells over the same routes; the SPA keeps its own
  routes where they serve UI only, and uses these where the data is shared.

### 2.2 Identity is the bearer

What `octo` can see and do is decided entirely by the token it presents. The
commands do not know or care who is calling.

| Caller | Bearer | Scope |
|---|---|---|
| A session's agent (and its sub-agents, and anything its shell runs) | The session's existing MCP bearer, `mcp_identity.mint(session_id, installation_id, user_id)` — signed, stateless, already carries the session and its owner | That session's account; actions attributed to that session |
| An Application backend | Its app token, extended to carry the permissions its manifest declares (§2.3) | Those permissions, in its owner's account |
| A person in a terminal | Their account's bearer (`octo login`, as the SPA's sign-in) | Their account |

The bearer reaches the process as `OCTO_TOKEN` and the base URL as
`OCTO_API_BASE`. Inside a session these come from the same assembly that already
builds the MCP entries, and the base URL is the runtime's `callback_base` — the
server as seen from wherever the session runs. That is already a designed
direction of the Runtime interface
([`runtime-providers.md`](https://github.com/archeryue/Octopus/blob/runtime-provider/docs/plans/runtime-providers.md)
§7, on the `runtime-provider` branch), so `octo` works unchanged on the local
box and inside a tenant's VM.

### 2.3 Permissions

A short list of scopes, checked by the interface layer:

```
sessions:read  ledger:read  ledger:write  board:read
ontology:read  ontology:write  schedules:read  schedules:write
agents:read  agents:ask  apps:data
```

- A **session** gets its Agent's tool policy translated into scopes. By default it
  gets read access across its own account, plus write access to its own Ledger
  item and its own schedules.
- An **Application** declares the scopes it needs in its manifest. The user
  confirms them when installing or forking, and the app token carries exactly
  those. This is the same permission declaration the template marketplace needs
  — one mechanism, built once.
- A **person** has everything their account has.

### 2.4 Designed for agents first

- **JSON by default**, `--human` for tables; stable field names; non-zero exit
  codes that mean something (`2` usage, `3` not found, `4` forbidden).
- **Progressive help.** `octo --help` lists the nouns; `octo ledger --help` the
  verbs. A model asks for what it needs when it needs it, instead of every MCP
  tool schema riding in every turn. A short skill text tells agents `octo` exists
  and when to reach for it.
- **Bounded output.** Lists are paged; long fields are spilled behind `--full` or a
  follow-up `show`. This is the Inline & Spill discipline from
  [`agent-router-and-channels.md`](agent-router-and-channels.md) §2, applied
  to a CLI.
- **Reads are safe to retry; writes are idempotent** where an id makes them so.

### 2.5 Commands (first cut)

```
octo whoami
octo agents   list | show <agent>
octo sessions list [--agent A] [--status S] | show <session> | read <session> [--tail N]
octo ledger   list [--agent A] [--status S] [--origin O] | show <item> | note <item> <text>
octo board    [--agent A]                       # the Ledger's current view
octo schedule list | show | create | update | delete
octo bg       run <command> [--description D] | list | show <task>
octo ask      <agent> <request> [--files …]     # agent-to-agent, as mcp__ask_agent__ask
octo ontology search <query> | get <entity> | put <entity> …
octo apps     list | data get|put <key> …       # an Application's own store
octo monitor  overview | turns | errors | …     # replaces reading octopus-metrics.db
octo devices  | device show|exec|cp|forward|notify …   # machines the user connected
```

The `device` verbs, and the `--device` option of `octo bg run`, are specified
in [`devices.md`](devices.md). Device control is part of this CLI, not a
separate tool: the same interface layer, bearers and scopes, extended with
per-device grants.

`octo monitor` moves the reports of today's `octopus monitor` behind the
interface layer, so even operational reads stop opening the database.

### 2.6 `octo` and `octopus`

- **`octopus`** (`server/cli.py`: `serve`, `handoff`, `pull`) runs and administers
  a server. It stays as it is.
- **`octo`** is the everyday client for agents, Applications and people. It
  ships in the same Python package first, as a second console script.
- It is designed to become a **single static binary** later. A copy in every
  tenant VM image costs a few MB and no interpreter
  ([`runtime-providers.md`](https://github.com/archeryue/Octopus/blob/runtime-provider/docs/plans/runtime-providers.md)).
  The interface is the contract, so the client's language can change without
  any consumer noticing.

### 2.7 `octo` and MCP

- **Data and actions — Ledger, sessions, Board, Ontology, schedules, apps:**
  `octo` first. Any agent with a shell can use it — Claude, Codex, sub-agents,
  scripts, an Application's `start.sh` — and it costs no per-turn tokens until
  used.
- **What needs the turn machinery stays MCP.** That is `bg` (its result comes
  back as a new turn), `ask` (asking the user, which blocks on the UI), and the
  delegation tools' reply injection. Their bodies call the same interface layer.
- Whether a namespace that is pure data (e.g. `schedule`) is retired in favour of
  `octo` is decided by measurement: per-turn tokens, and how often a model picks
  the right tool. It is not decided by preference.

---

## 3. Order of work

1. **The interface layer for what exists.** `/api/v1` routes for sessions,
   agents, schedules, bg tasks and monitor reports, with scopes and tenant
   checks; `octo` covering them; the session bearer and base URL injected into
   every turn's environment; the skill text.
   *Done when* the nightly SRE check runs on `octo` alone, without opening a
   database.
2. **The Ledger and Board routes**, built on this layer from day one
   ([`agent-router-and-channels.md`](agent-router-and-channels.md) §7 step 1).
3. **Application permissions**: manifest scopes, the confirm step, app tokens
   that carry scopes, `apps:data`. Then the official Board Application on top.
4. **MCP namespaces re-pointed** at the interface layer, and the measurement of
   §2.7.
5. **Ontology routes** with the Ontology plan.
6. **Static binary**, when a VM image wants it.
7. **Devices** ([`devices.md`](devices.md)), on top of step 1.

Every step keeps the direct path of a hand-made session unchanged. A session
that never runs `octo` pays nothing for its existence beyond two environment
variables.

---

## 4. Security

- No client reads a database; the interface layer is the only reader, so tenant
  scoping exists in one place.
- Bearers are scoped and attributable: a write records which session,
  Application or person made it.
- The session bearer is today stateless and lives as long as the master key.
  Before Applications and VMs hold it, decide its lifetime and revocation
  (§5).
- An Application's scopes are confirmed by a person and shown on its page; a
  fork does not inherit a grant it was not given.

## 5. Open questions

1. Session bearer lifetime and revocation: valid for the session's life, or
   short-lived and refreshed by the server per turn?
2. Whether `/api/v1` is a separate surface or the existing routes tightened. A
   separate surface keeps the SPA free to change; tightening avoids two routes per
   operation.
3. Which MCP namespaces, if any, retire in favour of `octo` (§2.7), by
   measurement.
4. Rate limits per bearer kind, once Applications call it on a loop.

## 6. What this defers

- A public, third-party API. `/api/v1` serves Octopus's own agents,
  Applications and users. Opening it to outside developers is a later product
  decision.
- Language bindings beyond the CLI. An Application backend in any language can
  call `octo` or the HTTP surface directly.
