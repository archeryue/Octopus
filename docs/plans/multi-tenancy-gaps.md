# Multi-tenancy, second pass: Connectors, Schedules, Harness

> **Status:** in-progress — Proposal: close the gaps a read-only review found where accounts can reach each other's work through the Manage surfaces, and make each class of gap structurally impossible.

> **Implementation status: PROPOSED (2026-10-04), nothing built.** A read-only
> review of the three Manage surfaces — Connectors, Schedules, Harness — on
> `origin/main` at `6d6ba0b` (the deployed user-1.0 build), with read-only counts
> from the deployed database. Every problem marked *reproduced* was run against
> throwaway in-memory databases with two accounts; nothing in the repo, the
> deployed database or the service was changed. (First published as
> `connector-tenancy.md`, covering Connectors only; renamed when the review
> widened.)
>
> Google's "Testing" publishing status (test-user list, 7-day refresh tokens,
> `gmail.modify` verification) is **out of scope** by decision.

Related: [`multi-tenancy.md`](multi-tenancy.md) (the design this checks against),
[`connectors.md`](connectors.md), [`scheduled-runs.md`](scheduled-runs.md),
[`schedule-tool.md`](schedule-tool.md), [`harness-layer.md`](harness-layer.md),
[`codex-backend.md`](codex-backend.md), [`polish-2026-09.md`](polish-2026-09.md)
§4 B1, [`token-rotation.md`](token-rotation.md) §6,
[`sidebar-pins.md`](sidebar-pins.md) §1.

---

## 1. Verdict

**The architecture is right; the implementation has gaps, and some are live.**

What `multi-tenancy.md` designed holds up under review: install-level things
(connector kinds, OAuth clients, the scheduler process, the harness profiles)
are shared on purpose; per-person things (installations, credentials, agents,
schedules, sessions) carry `user_id`; secrets are under per-account DEKs; REST
routes are scoped through `ScopeUser` and almost all of them do it right. None
of the fixes below change that model.

The gaps are not random. Almost every one is an instance of five classes, and
the plan fixes the classes, not just the instances:

| Class | What goes wrong | Instances |
|---|---|---|
| **A. Ownership checked on read, not on write/use** | A route lists only your rows, but an id you pass in — a session, a credential — is accepted without asking whose it is, or a background path loads it without asking | C4, S1, S5, H2, H3, X1, X2 |
| **B. Shared process state after the in-process move** | State that was per-installation because each sidecar was its own process became shared when one mount served everyone | C1, C2 |
| **C. A disabled account is only signed out** | `disabled_at` is checked at sign-in and token resolution only; schedules, MCP bearers, held processes and credentials carry on | C5, S3, H5 |
| **D. A missing credential silently becomes the operator's** | A turn whose credential is absent or unusable runs on the host's own `claude` / `codex` login | H1, S6 (and the fallout of H3, H7, H9) |
| **E. Frames and caps that are install-wide by accident** | A broadcast with no owner goes to everyone; a cap counted in one path and not another | S2, S8, S4, H4 |

## 2. Facts from the deployed install (read-only, 2026-10-04)

| | |
|---|---|
| Accounts | 4 — 1 admin, 1 active member, 2 disabled |
| Gmail installations | **2, owned by 2 different accounts** |
| GitHub installations / custom kinds | 1 / 0 |
| Credentials | 4 (Claude OAuth + Codex for each of the 2 active accounts), none `needs_reconnect` |
| Schedules | 6, all enabled, all cron, all with an origin session (5 admin, 1 member) |
| Sessions pinned to a credential id that **no longer exists** | 2 — admin's live session (164 completed turns) and member's archived one (12) — both ran on the **host login** |
| Disabled accounts' turns in the last 7 days | 2, both on the **host login** (no credential of their own) |
| Cross-owner links (agent↔installation, agent/session↔credential, schedule↔session) | 0 |

Nothing in the data shows the holes being used. The rows that matter are the
first Gmail row (C1 is live) and the two host-login rows (D is live, and has
been for weeks).

---

## 3. Connectors

### C1 — The token cache is shared by every installation of a kind 🔴 live, reproduced

`server/mcp_servers/connectors/gmail.py:41` (and `github.py:42`, `custom.py:40`)
creates one module-level `ctx = ConnectorContext()`. Its cache (`_shared.py:67`)
is two bare fields, `_access_token` / `_token_exp`, with no installation in the
key. Since B1 every kind is served from **one** in-process mount for every
session of every account, and tool bodies run concurrently on anyio's worker
threads (`mcp_http.py:82`, limit 40).

Archer's agent calls a Gmail tool → `ctx` caches Archer's token for ~1 h. Within
that hour Vera's agent calls a Gmail tool; its scope correctly names Vera's
installation, but `access_token()` returns **Archer's** cached token. Google
answers 200, so nothing fails: Vera's agent reads — and via `send_draft` sends
from — **Archer's mailbox**. Reproduced with the real `ConnectorContext`: the
second installation received the first's token and never fetched its own. Also:
one account with two Gmail accounts gets the wrong mailbox; GitHub likewise; two
*different* custom kinds share a module, so one service's token goes to another
service's API. No test covers two installations.

**Root cause (class B).** The connector servers were written for one process per
installation, where a module global was per-installation by construction. B1
moved the installation id into the call's scope (`_host.resolve`) but not the
state that had been implicitly per-process.

### C2 — Custom connectors are broken in-process and can leak their token 🔴 latent

* `custom.py`'s `_api_base()` reads `os.environ["OCTOPUS_CONNECTOR_API_BASE"]`
  directly. Only the stdio `mcp_entry` (`server/connectors/custom.py:165`) set
  it, and production always takes the HTTP path (`harness/run.py:245`), so every
  custom call answers "misconfigured (no API base)". Latent: 0 custom kinds.
* `request` accepts an absolute URL (`path.startswith("http")`), attaching the
  installation's token to **any host** — a prompt injection can exfiltrate it.

### C3 — The OAuth callback is not bound to the browser that started it 🟠

`oauth_callback` (`routers/connectors.py:252`) trusts the pending login's
`user_id` and checks only `state`. An attacker can start a login in their own
account and send the Google consent URL to a victim; if the victim consents,
**the victim's Gmail is installed into the attacker's account**.

### C4 — Ownership checked when a link is written, never when it is used 🟡

`set_agent_connector` / `replace_agent_connectors` check ownership, but the
turn-time loader (`db/agents.py:52`, from `sessions/turns.py:1362`) joins on
`agent_id` alone. `/token` would still refuse a foreign installation, so this is
not a leak — but a cross-owner link would put another account's name and tools
into an agent's prompt.

### C5 — A disabled account keeps using its connectors 🟡 latent

`get_access_token` still refreshes and returns a disabled owner's token; their
schedules and turns still call it (class C, §7.3).

### C6 — Disconnecting does not revoke at the provider 🟡

`delete_installation` drops local rows only; the refresh token stays valid at
Google (and the GitHub grant authorised).

### C7 — Every refresh failure is treated as revocation 🟡

`get_access_token` catches **any** exception from `provider.refresh` and flips
`needs_reconnect` — a timeout or a Google 503 looks like `invalid_grant`. No
monitor event on that path. Conversely a provider-API 401 marks the installation
broken without trying a refresh first.

### C8 — Dead and misleading code 🟢

The stdio connector path (`ConnectorBase.mcp_entry`, `OCTOPUS_INSTALLATION_ID` /
`OCTOPUS_CONNECTOR_API_BASE` env) has no production caller; `enable_by_default`
is stored and never applied; docstrings still say *"Installations are global"*
(`routers/connectors.py`) and describe a *"boot-time refresh sweep"* that does
not exist (`connector_manager.py`, `db/connectors.py`).

---

## 4. Schedules

### S1 — A schedule can fire into another account's session 🔴 live, reproduced

`origin_session_id` — the session a fire appends into — comes from the request
and is never checked for ownership:

* `POST /api/schedules` (`routers/schedules.py:219-237`) resolves `session_id`
  only when `agent_id` is absent; with both given, the session is not checked.
* `POST /api/agents/{id}/schedules` (`agents.py:278-284`) and `…/from_text`
  (`agents.py:329-339`) pass it straight through.
* At fire time `scheduler.py:139` looks the session up **without an owner** and
  `start_message` (`turns.py:331`) does not check either.

Reproduced: Archer's schedule pointed at Vera's session was accepted (201) on all
three routes; firing it queued Archer's prompt into Vera's session — run with
Vera's agent, credential, connectors and workspace, billed to Vera, every tick.
It needs Vera's session id, which S2 broadcasts to every browser.

### S2 — `session_archived` frames go to every account 🟠 reproduced

The four `session_archived` broadcasts (`sessions/lifecycle.py:237, 274, 310,
345`) carry `old_session_id` / `new_session_id` and the session name but no
`session_id` or `user_id`, so `_audience` (`sessions/base.py:536-551`) treats
them as install-wide. Every scheduled fire auto-archives its session
(`scheduler.py:202`), so every fire tells every connected browser a session id
and "*agent name — timestamp*"; archive-with-replacement also leaks the live
successor's id — which is what makes S1 and X1 practical.

### S3 — A disabled account's schedules keep firing 🟠 latent, reproduced

`_fire` (`scheduler.py:103-205`) never checks `users.disabled_at`, though the
schema comment promises a disabled account's "sessions and schedules stop
running" (`db/schema.py:443-444`). Class C.

### S4 — Throwaway fires skip the per-user turn cap 🟠 reproduced

The fresh-session path calls `send_message` directly (`scheduler.py:190`); only
`start_message` checks `max_concurrent_turns_per_user` (`turns.py:336`). In the
origin-session path a cap refusal is swallowed by the generic `except`
(`scheduler.py:161`) with no event. With no global cap (H4), one account's
schedules can crowd out everyone's memory.

### S5 — An agent's credential is never checked for ownership 🟠

Covered as H2 (agents are one of its write paths); listed here because every
scheduled fire and every `/schedule` text parse (`agents.py:312`) runs on — and
bills — the agent's credential.

### S6 — Codex agents' scheduled fires run on the Claude backend 🟡 reproduced

`_fire` calls `create_session(agent_id, origin="schedule")` with no backend
(`scheduler.py:176`), defaulting to `claude-code` (`lifecycle.py:64`); the agents
route inherits the agent's backend (`agents.py:236-240`). A Codex credential
cannot be used Claude-style, so the run falls to the host Claude login (class D).
Latent: all 6 scheduled agents are Claude.

### S7 — Archived or deleted agents' schedules keep firing 🟡 reproduced

Archiving neither removes nor pauses the agent's jobs (`db/agents.py:279-290`,
`agent_manager.py:120-126`), and `create_session` accepts archived agents
(`lifecycle.py:82`) — contradicting `sidebar-pins.md` §1 ("archived — out of
use: delegation, schedules"). A deleted agent's job stays registered and fails
each tick as `overlap_skip` (`scheduler.py:193-196`) until restart.

### S8 — `schedules_changed` goes to everyone 🟢 reproduced

`_broadcast_change` (`schedules.py:72-88`) has no owner. It carries no data and
each browser refetches its own scoped list, so only a timing signal leaks.

**Already right in Schedules:** REST list/update/delete scoped through the
agents join; agent-scoped list/create check the agent first; session-scoped
routes derive the agent from an owned session; the `schedule` MCP namespace has
no module state; a fire's owner, workspace (`confine`) and DEK follow the agent;
the overlap guard and job ids are per schedule; timezones and `run_at` are
per-schedule.

---

## 5. Harness (backends, credentials, the processes that run turns)

### H1 — A turn with no usable credential runs on the operator's own CLI login 🔴 live

`resolve_credential_by_id` (`sessions/credentials.py:74-121`) returns `None` for
six different reasons — no id, missing row, `needs_reconnect` (logged as
"running without auth override"), undecryptable, Codex without `auth.json`, a
Codex `api_key` row. On `None`, Claude gets `os.environ.copy()` with no token
(`harness/claude_code.py:328-329`, `:176-185`) and Codex gets no `CODEX_HOME`
(`harness/codex.py:115-120`) — so the CLI uses the service user's own
`~/.claude/.credentials.json` / `~/.codex/auth.json`. Nothing refuses once
accounts exist; the UI offers it as **"Host default sign-in"**
(`web/src/components/CredentialPicker.tsx:59,70,114`).

Silent routes into it: a new account's agents have no credential; re-auth marks
a credential `needs_reconnect` (`turns.py:1046`); deleting a credential NULLs it
on agents and sessions; a **dangling** `sessions.credential_id` still counts as
"set" and so wins over the agent's real credential (`credentials.py:42`), and
`create_session` accepts unknown ids (`routers/sessions.py:120-121`).

Deployed: the disabled accounts' turns ran on the host login; the admin's live
session (164 turns) and the member's archived one are pinned to a deleted
credential and have been running on the host login although their agents have
credentials. `multi-tenancy.md` §9 already names "runs the turn *without* the
user's credential — silently" as the worse half of the deployment bug; this is
the same outcome by a different road.

### H2 — Another account's credential can be attached to your session or agent and used 🟠 reproduced

`get_credential(cred_id)` is called **without an owner** on five write paths —
`update_session` (`routers/sessions.py:155`), `_check_credential_backend`
(`:120`), `create_session` / `import_session` (`:212`, `:239`), agent
create/update (`routers/agents.py:75`, `:122`; `agent_manager.py:58`, `:121`).
At use, the Claude secret is decrypted with the **row owner's** key
(`credentials.py:113`) with no check that the owner is the session's. Reproduced:
Archer's `sk-ant-…` ended up in Vera's `ANTHROPIC_API_KEY`, where her agent can
print it. Codex is worse: its home is resolved with no database read at all
(`credentials.py:77-83`), so any id runs on that `CODEX_HOME` (subscription and
history). Needs the 12-hex id, which no API reveals across accounts — but
isolation level A puts it on disk.

### H3 — Claude re-authorisation can overwrite another account's credential 🟠 reproduced

`oauth_complete` with a `credential_id` looks it up **unscoped**
(`routers/credentials.py:348`) and writes the caller's token, encrypted under
the *caller's* key, into the victim's row — which then cannot be decrypted, so
the victim's turns drop to the host login (H1). The Codex equivalent is scoped
(`:459`).

### H4 — Turn caps are incomplete, and hitting one silently loses work 🟠

* The per-user cap (4) is checked only in `start_message` (`turns.py:281-301`,
  `:336`). Throwaway schedule fires (S4), research leaves (`research/leaf.py:70`,
  `:121`) and one-shots are not counted. **There is no global cap**, which
  `multi-tenancy.md` §8 promises.
* `QuotaExceeded` is not a `ValueError`, so injection paths catch it as a
  generic failure and **drop the payload**: bg results (`turns.py:463-470`),
  delegation replies/questions (`delegations.py:853`, `:1001`), research reports
  (`research/manager.py:216`), scheduled fires (`scheduler.py:150-162`). A
  delegation reply is injected from inside the child's running turn
  (`base.py:553-567`), so an agent with 4 children running loses the first reply.
* The held-process pool is one global LRU of 4 (`sessions/base.py:47`,
  `processes.py:42-79`) — one account evicts another's; `stop_all_held_processes`
  is still global (`processes.py:138`), not per-user as §8 says; the research
  semaphore is a global 2 (`research/manager.py:76`).

### H5 — A disabled account keeps acting 🟠 latent

`set_disabled` revokes login tokens only (`users.py:298-306`): running turns and
held processes continue, the scheduler fires (S3), and MCP scope bearers — which
**never expire** and authorise every REST and WebSocket route as their account
(`auth.py:40`, `deps.py:180`, `:205`) — keep working. They sit in the CLI's argv
(`claude_code.py:256`, `:294`) and Codex's env (`codex.py:132`), so a disabled
user who kept one can still start turns — on the host login (H1). Class C.

### H6 — Codex sign-ins are not stored per account 🟡

`codex_home_for` is `settings.codex_home_dir/<cred_id>` (`codex_login.py:79-84`)
— one pool for everyone — while `paths_for(uid).codex_home`
(`workspace.py:98`) is created and never used, contradicting `multi-tenancy.md`
§6.

### H7 — Cancelling or failing a Codex re-auth destroys the working sign-in 🟡

Re-auth reuses the credential's own directory (`codex_login.py:127-128`); `_fail`
and `cancel` `rmtree` it (`:249`, `:262`) — the valid `auth.json` and every
rollout — and the account silently drops to the host login (H1). Not a tenancy
bug, but it feeds class D.

### H8 — Token rotation is reachable by any account 🟡

Beyond the known gap (`multi-tenancy.md` §11: rotation re-keys every secret with
the install token and so fails once accounts store any), `POST /api/auth/rotate`
is guarded only by `verify_token` (`routers/auth.py:359`) — any account or MCP
bearer can call it. It rewrites and restores the operator's env files before
failing (`token_rotation.py:176-191`); if it ever succeeded it would stop every
account's processes (`:272-286`). The route-scoping test exempts it on the
premise that it "exists only before accounts do" (`tests/test_route_scoping.py:60`).

### H9 — Claude OAuth refreshes can race 🟡 suspected

`_refresh_oauth_if_needed` (`credentials.py:150-249`) has no per-credential lock;
two turns near expiry both refresh, and the loser can mark a freshly refreshed
credential `needs_reconnect` (`:213-218`) → H1. Depends on whether Anthropic
rotates refresh tokens on use.

### Accepted by design, worth cheap hardening

`multi-tenancy.md` §2 accepts isolation level A (an agent can read the box). Two
things inside that envelope are still worth narrowing because they cost almost
nothing: every CLI child inherits the **whole server environment**, including
`OCTOPUS_AUTH_TOKEN` and `OCTOPUS_MASTER_KEY_FILE` (`run.py:111`,
`claude_code.py:328`, `codex.py:205`); and the host `~/.claude` (transcripts,
skills, settings) is shared and writable by every account's turns.

**Already right in the Harness:** credential list/patch/delete and the Codex
login flow are owner-scoped; the in-memory login managers record and check who
started a login; secrets are under the owner's DEK and `_CREDENTIAL_COLS`
selects `user_id`; the held-process signature includes the credential;
`GET /api/backends` is read-only; `CLAUDE_CONFIG_DIR` is never set.

---

## 6. Cross-cutting, found on the way

### X1 — The WebSocket acts on any session id it is given 🔴 live

The socket subscribes *as an account* (`routers/ws.py`, `on_broadcast(…,
user_id)`), so frames are routed correctly — but its actions take `session_id`
from the client and call the manager **without an owner check**:
`send_message` → `start_message`, `interrupt`, `approve_tool`, `deny_tool`,
`answer_question` (`routers/ws.py:86, 110-147`). With a session id from S2's
broadcast, any signed-in account can type into, interrupt, approve tool calls
in, or answer questions for another account's session.

### X2 — Archiving or deleting an agent acts before checking ownership 🟠 reproduced

`POST /api/agents/{id}/archive` calls `archive_agent(agent_id)` and
`evict_agent_sessions(agent_id)` **before** its scoped lookup
(`routers/agents.py:134-139`; `agent_manager.py:120-126`) — reproduced: Archer
archived Vera's agent and evicted her sessions, and got a 404.
`DELETE /api/agents/{id}` calls an unscoped `delete_agent` (`agents.py:196`,
`agent_manager.py:198`), deleting another account's session-less agent and then
answering 500. `test_writing_to_someone_elses_row_is_refused` passes because it
checks only the status code.

---

## 7. Design

The fixes are organised by class, because each class gets one mechanism and a
test that makes the next instance fail on arrival.

### 7.1 One ownership rule, on write *and* on use (class A)

> **Any id that arrives from a caller, or is read back from a row, is resolved
> through the owner before it is acted on.**

* **Scoped accessors only, on every path that acts.** `get_session(id,
  user_id)`, `get_credential(id, user_id)`, `get_agent(id, user_id)` — never the
  unscoped form — in: schedule create/update (S1), session create/update/import
  and agent create/update (H2), `oauth_complete` (H3), agent archive/delete
  (X2, look up first, act after), and every WebSocket action (X1, resolve
  `get_session(sid, user_id)` once per message and refuse otherwise).
* **Same-owner invariants at use**, for links that a row carries:
  a schedule's origin session must belong to the agent's owner and agent (S1 —
  checked at fire, falling back to a throwaway session on mismatch); a session's
  or agent's credential must belong to the same owner (H2 — checked in the
  resolver, including Codex, which must read its row); an agent's enabled
  installations must share its owner (C4 — in the loader's join).
* **Reject dangling ids at write time** (H1's dangling-pin route): an unknown
  credential id is a 400, not a stored value.
* **Structural test.** `tests/test_route_scoping.py` already asserts every
  route takes a scope. Add its write-side twin: for each route that accepts a
  foreign id (`session_id`, `credential_id`, `agent_id`, `installation_id`,
  `origin_session_id`), a two-account test passes the *other* account's id and
  asserts both the refusal **and that the other account's row is unchanged** —
  the status-code-only check is what let X2 through.

### 7.2 No state in a shared mount (class B)

> **A tool call's identity is `(user_id, session_id, installation_id)` from its
> verified scope, and nothing in a connector server outlives one call.**

* `ConnectorContext` becomes stateless: every property resolves from the scope;
  `access_token()` asks the host every time. The loopback `/token` route stays
  the one authorisation point (it checks the installation's owner); its ~1–2 ms
  is noise beside a provider round trip and keeps tool bodies off the event loop.
  No server-side cache in this change — a decrypt per call is cheap and leaves
  nothing to get wrong (C1).
* `/token` returns the kind's non-secret `api_base`; every `os.environ` read in
  connector servers goes. `request` accepts only relative paths and refuses any
  joined URL whose scheme, host, port or path prefix differs from the base (C2).
* **Structural test:** import every module under `server/mcp_servers/` and fail
  if any module-level object other than `mcp` holds mutable state.

### 7.3 Disabled means off, everywhere (class C)

One predicate, `UserManager.is_active(user_id)`, consulted where an account can
act without signing in: the scheduler's `_fire` (skip, `schedule_skipped` event
with reason `owner_disabled`; S3), `auth._allowed` / `scope_user_id` for MCP
scopes (H5 — also stops a mid-flight turn at its next tool call),
`get_access_token` (C5) and credential resolution. `set_disabled(True)` also
stops the account's running turns and held processes. MCP scope bearers gain an
expiry (the turn's lifetime plus slack) so a captured one stops working on its
own. Re-enabling restores everything; nothing is deleted.

### 7.4 No silent fallback to the operator's login (class D)

* `resolve_credential_by_id` returns a **typed outcome** — `Resolved(cred)`,
  `Missing`, `NeedsReconnect`, `Unusable(reason)` — instead of `None` for six
  different things.
* Once accounts exist, anything but `Resolved` **refuses the turn** with an
  actionable error ("connect Claude on the Harness page", "reconnect your Codex
  sign-in") — for interactive turns, scheduled fires, one-shots (`/schedule`
  parse) and research leaves alike.
* Using the host's own login becomes an **explicit, admin-granted per-account
  flag** (`users.may_use_host_login`), true for the admin who owns the box and
  false otherwise; the picker shows "Host sign-in" only to accounts holding it.
  This is the one decision this plan needs from the operator (§10).
* Causes that feed it are fixed at the source: schedule fires inherit the
  agent's backend and model (S6); Codex re-auth runs in a temporary directory and
  swaps `auth.json` in only on success (H7); Claude refresh takes a
  per-credential lock and re-reads the row after acquiring it (H9); Codex homes
  live under `paths_for(owner).codex_home/<id>`, with a one-time move of the two
  existing directories (H6).

### 7.5 Owners on frames, one gate for turns (class E)

* **Frames.** Every broadcast states its owner: the four `session_archived`
  sends pass `user_id=old.user_id` (S2), `schedules_changed` passes the agent's
  owner (S8). Structural test: every frame type that reaches a socket either
  resolves an owner or is on an explicit, reviewed list of install-wide frames.
* **Turns.** One turn-slot gate inside `send_message` — the point every path
  passes through — enforcing the per-user cap and a **global** cap; schedule
  fires, research leaves and one-shots are counted (S4, H4). System injections
  (bg results, delegation replies, research reports, origin-session fires)
  **queue** behind the gate instead of raising, so hitting a cap delays work and
  never loses it. A fire that cannot run records a `quota_skip` event.
* **Processes.** The held-process pool is capped per user and globally;
  `stop_held_processes(user_id)` replaces the global stop; research concurrency
  becomes per user.

### 7.6 The rest

* **OAuth callback bound to its browser (C3).** `oauth/start` sets
  `octopus_oauth_bind=<random>` (HttpOnly, Secure on https, SameSite=Lax,
  `Path=/api/connectors/oauth`, 15 min) and stores its hash on the pending
  login; the callback requires a constant-time match. Lax is exactly right:
  Google's redirect back is a top-level GET. A flow started on the laptop cannot
  finish on the phone — the property we want.
* **Revoke on disconnect (C6).** Optional `ConnectorOAuthProvider.revoke`
  (Google `POST oauth2.googleapis.com/revoke`; GitHub `DELETE
  /applications/{client_id}/grant`; custom kinds an optional `revoke_url`).
  Delete revokes first; a revoke failure still deletes and is reported
  (`revoked: false`) so the UI can say "also remove access in your Google
  account".
* **Refresh failures classified (C7).** `invalid_grant` / `invalid_client` /
  `unauthorized_client` → `needs_reconnect`; network, timeout, 429, 5xx →
  transient, flag unchanged. Both record a `connector_refresh` event. A provider
  401 in a tool forces one refresh (`/token?force_refresh=1`) and one retry
  before flagging.
* **Archived agents are out of use (S7).** Archive/delete remove the agent's
  scheduler jobs (unarchive re-adds them); `_fire` and `create_session` refuse
  archived agents.
* **Rotation (H8).** `POST /api/auth/rotate` answers 409 once accounts exist
  (the operator rotates the master key, not the install token); in the
  pre-accounts era it re-keys only `user_id IS NULL` rows, as `token-rotation.md`
  §6 prescribes. The route-scoping exemption's premise becomes true.
* **Child environment (hardening).** CLI children get an allowlisted
  environment, as app backends already do (`app_backends.py:102`): no
  `OCTOPUS_AUTH_TOKEN`, no master-key path.
* **Removals (C8).** The stdio connector path, `enable_by_default` (auto-attaching
  a mailbox with `send_draft` to every new agent is the wrong default), and the
  stale docstrings.

### 7.7 Data and API changes

| Change | Kind |
|---|---|
| `users.may_use_host_login` | add, default false (true for the claiming admin) |
| `connector_installations.enable_by_default` | drop (guarded migration) |
| `custom_connectors.revoke_url` | add, nullable |
| Codex homes | move to `users/<id>/codex/<cred_id>` (one-time) |
| MCP scope bearer | gains an expiry |
| `GET /api/connectors/{id}/token` | adds `api_base`; accepts `force_refresh` |
| `POST /api/connectors/oauth/start` / `oauth/callback` | set / require the bind cookie |
| `DELETE /api/connectors/{id}` | revokes; returns `{revoked, detail}` |
| `POST /api/auth/rotate` | 409 once accounts exist |
| Credential / session / schedule writes | 400 on an unknown id, 404 on another account's |
| WebSocket actions | error frame on a session that is not the caller's |

No change to the ownership model, the key hierarchy, or which tables have owners.

---

## 8. Verification

**Structural (the ones that stop the next instance):** write-side two-account
test for every id-taking route, asserting the other row is unchanged (§7.1); no
module-level state in any MCP server (§7.2); every socket frame has an owner or
is on the reviewed install-wide list (§7.5); `is_active` consulted on every
path in §7.3 (a test per path).

**Behavioural:** C1 two installations alternating (two accounts, and two in one
account; Gmail, GitHub, custom); C2 base resolved in-process, absolute URL /
other host / `..` / scheme change refused; C3 callback without, and with
another login's, bind cookie refused; S1 schedule with a foreign session refused
on all three routes and re-routed at fire; S2/S8 frames reach only their owner;
X1 every WebSocket action on a foreign session refused; X2 foreign archive/delete
refused and the row untouched; H1 every non-`Resolved` outcome refuses the turn
for an account without the flag and runs it for one with; H2/H3 foreign
credential refused at write and at use; H4 caps hold across all paths and a
capped injection is delivered later, not lost; S6 a Codex agent's fire runs on
Codex; S7 archived agent's schedule removed and restored on unarchive; H7
cancelled re-auth leaves the old sign-in working; C6/C7 revoke and refresh
classification; H8 rotate refused with accounts.

**Against the real thing:** a rehearsal on a snapshot of the deployed database
(`multi-tenancy.md` §10's procedure) with both real Gmail installations — plant
a uniquely-subjected message in each mailbox and have each account's agent
search for both, alternating, inside one token lifetime; each must only find its
own. (Safe with copied tokens: Google does not rotate refresh tokens on use and
GitHub OAuth tokens never refresh; they are still cleared afterwards.) The same
rehearsal confirms the two dangling-pin sessions now refuse with an actionable
error instead of running on the host login. Real-CLI tier: overlapping Gmail
turns from two accounts. e2e: the bind-cookie flow and disconnect's revoke
result; a second account's browser cannot act on the first's session.

## 9. Order of work

Live holes first, each change shipping with its structural test:

1. **X1 + S2 + S1** — the socket, the id leak that feeds it, and the schedule
   route into another account's session. One change: they are one attack.
2. **C1 + C2** — the shared connector state. *Operational stopgap until it
   ships: enable Gmail on one account's agents only.*
3. **H1 + S6 + H7 + H9 + H6** — no silent host login, and the causes that feed
   it. Before shipping, repoint the two dangling-pin sessions to their agents'
   credentials so they do not start refusing out of the blue.
4. **X2 + H2 + H3 + C4** — the remaining ownership-on-write gaps.
5. **H5 + S3 + C5** — disabled means off; scope expiry.
6. **H4 + S4 + S8** — one turn gate, global caps, owned frames.
7. **C3, C6, C7, S7, H8**, hardening, removals.

Each step passes the full suite (hermetic, real-CLI, both e2e buckets) on its
own and is rehearsed against the snapshot where it touches live data.

## 10. What this defers

* **Google publishing** — leaving "Testing" (test users, 7-day refresh tokens,
  `gmail.modify` verification / CASA). Deferred by decision.
* **The host-login policy default** needs the operator's decision (§7.4): which
  accounts, if any besides the admin, may run on the box's own Claude / Codex
  subscription. The mechanism is in this plan; the default is the operator's.
* **Account deletion and crypto-shred.** `multi-tenancy.md` §4 describes
  deletion as dropping the DEK, but no delete-account path exists; when built it
  calls §7.6's revoke for each installation first. Needs a product decision.
* **Filesystem isolation between accounts** beyond the child-environment
  allowlist — the shared host `~/.claude`, readable workspaces, `/proc` — is
  isolation level A by design (`multi-tenancy.md` §2) and belongs to the
  per-tenant-container version.
* **Persisting pending OAuth logins** (in memory, 15-minute TTL) — worth it only
  with more than one server process.
* **Per-account connector quotas.** Google's API quota is per OAuth client;
  revisit with the publishing work.
