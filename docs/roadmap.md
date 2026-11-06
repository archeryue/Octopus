# Roadmap — back in the US (from 2026-12)

What to build when Archer is back in the US, in his order. Each item links to
the plan that holds its design; this page is the list, not the design.

Suggested sequencing, by dependency rather than by number:

1. Item 1 first: it is the precondition for anyone but the operator using an
   install.
2. Item 4, step 1 next: it is step 0 of item 3.
3. Then items 3 and 2 can run in parallel.
4. Item 5 after item 4, step 1: device control is part of `octo` and stands on
   its interface layer, bearers and scopes.

---

## 1. Multi-tenancy: the bugs it left

Plan: [`plans/multi-tenancy-gaps.md`](plans/multi-tenancy-gaps.md) — every item
below has its ID, evidence and fix there; §7 is the design that closes them by
class.

**Connector isolation** (§3):

- **C1** 🔴 — the token cache is shared by every installation of a kind.
- **C2** 🔴 — custom connectors are broken in-process and can leak their token.
- **C3–C7** — OAuth callback not bound to its browser; ownership checked on write
  but never on use; disabled accounts keep their connectors; disconnect does not
  revoke; every refresh failure treated as revocation.

**Old interfaces that don't take or check the user** (class A, "one ownership
rule, on write *and* on use", §7.1):

- **X1** 🔴 — the WebSocket acts on any session id it is given.
- **S1** 🔴 — a schedule can fire into another account's session.
- **H2 / H3** — another account's credential can be attached, used, or
  overwritten on re-auth.
- **S5** — an agent's credential is never checked for ownership.
- **X2** — archiving or deleting an agent acts before checking ownership.
- Broadcasts to every account: **S2** `session_archived`, **S8**
  `schedules_changed`.
- Audit the remaining internal helpers still called without a `user_id` (the
  legacy single-user signatures), so scoping is never optional by default.

**Schedules and the harness** (§4, §5):

- **H1** 🔴 — a turn with no usable credential runs on the operator's own CLI
  login. No silent fallback (§7.4).
- **S3, H5** — disabled means off, everywhere (§7.3).
- **S4, H4** — turn caps incomplete; hitting one silently loses work.
- **S6** — Codex agents' scheduled fires run on the Claude backend.
- **S7** — archived agents' schedules keep firing.
- **H6–H9** — per-account Codex sign-ins, re-auth that destroys a working
  sign-in, token rotation reachable by any account, refresh races.

**Found in operation since** (recorded by the nightly SRE check):

- Invited accounts have no protected default agent (`is_system`), so their
  "Octo" can be unpinned or lost, and a session created without an agent has
  nowhere to go.
- The WebSocket bearer is logged in plain text (`/ws?token=…` in the access log).
- Harness errors: classify a turn by its final failure event, not a keyword
  scan of all output (a 400 `model_not_found` was retried as "transient"). Show
  the provider's raw error. Clear `needs_reconnect` when a re-login succeeds.
  Add a version / health check for the harness CLIs, so a stale CLI is caught
  before users hit it.

## 2. The Runtime architecture

Plan: [`runtime-providers.md`](https://github.com/archeryue/Octopus/blob/runtime-provider/docs/plans/runtime-providers.md),
on the `runtime-provider` branch; the platform side is
[Octopus-Runtime](https://github.com/archeryue/Octopus-Runtime) `docs/design.md`.

- **Done on the branch:**
  - steps 1–7: the `Runtime` interface, `LocalRuntime`, every spawn site through
    `exec()`, `expose` / `callback_base`, `confine` / `paths`, the ensure/release
    lifecycle;
  - the conformance suite.
- **Remaining:**
  - Rebase `runtime-provider` onto `main` and merge, keeping all suites green.
  - **Step 8** — the environment allowlist (the operator's `OCTOPUS_AUTH_TOKEN`
    stops reaching agents).
  - **Step 9** — fork as a runtime step.
  - **`runtime-1.1` file operations**, implemented in `LocalRuntime` first: the
    largest gap for any remote provider.
  - The standard as a **wire protocol**, so a provider in another language (a
    Rust in-VM agent) conforms; the conformance suite runnable over the network.
  - An `ensure` that can report "runtime offline", and every caller surfacing it.
  - Held-process policy per tenant (today `_MAX_HELD_PROCESSES = 4` box-wide).
- **Session history in the tenant's Runtime storage.** The writer stays the
  same: the Octopus server parses the CLI stream as it does today. Only *where*
  it writes moves behind the Runtime interface: append and read history as
  storage operations, like the `runtime-1.1` file operations. A server cannot
  open a SQLite file on another machine.
  - `LocalRuntime` implements it as one SQLite file per user, which also makes
    the single box physically isolated per account.
  - The site keeps metadata, the Ledger and rebuildable derived indexes
    (search). Migrating today's shared `messages` table needs a rollback path.
  - A Runtime is always online by assumption, so no "history unreadable"
    state exists. A Runtime down is an outage, recovered by the platform's
    storage/compute separation.
  - Fold this into `runtime-providers.md` when the branch merges.
- **Octopus-Runtime itself is not urgent:**
  - buy the bare-metal box;
  - k3s + Kata;
  - the owner's WSL2 node;
  - the controller and admin console.

## 3. The Router architecture

Plan: [`plans/agent-router-and-channels.md`](plans/agent-router-and-channels.md) §7.

1. The **Ledger**, and the **Board as an official Application**: bundled
   Applications, app scopes.
2. The **Agent Chat** and the **Router**: worker mode of delegation, slots,
   reclamation, rotation.
3. **Channels**: the adapter framework, one external source (GitHub or an IM),
   Threads and subscriptions.
4. Router **notes** to hand-made sessions, and **hand-over**.
5. **Watchdog**.
6. **Ontology**, with its own plan and official Application.

Invariant at every step: a hand-made session's time-to-first-token and per-turn
tokens do not change, enforced by a regression test.

## 4. `octo` — the CLI and its interface layer

Plan: [`plans/octo-cli.md`](plans/octo-cli.md) §3.

1. `/api/v1` for what exists (sessions, agents, schedules, bg, monitor), with
   scopes and tenant checks; `octo` over it; the session bearer and base URL in
   every turn's environment.
   *Done when* the nightly SRE check runs on `octo` alone.
2. Ledger and Board routes, built on it from day one.
3. Application permissions: manifest scopes, confirmation, scoped app tokens.
4. MCP namespaces re-pointed at the interface layer; measure which pure-data ones
   retire.
5. Ontology routes.
6. A static binary, when a VM image wants it.

## 5. Devices

Plan: [`plans/devices.md`](plans/devices.md) §6. Starts after `octo` step 1.

1. Shell devices: the `octod` daemon (pairing, outbound connection, `exec`,
   files, forward), the device registry and per-Agent grants, `octo device …`,
   Ledger records.
2. Long jobs on devices (`octo bg run --device`), queueing for offline devices,
   phone approvals for sensitive actions.
3. Phones as capability devices.
4. Cloud device providers, Lambda first.

A Device is never a Runtime: a Runtime is always online, a Device may go
offline at any time, and the two get different designs.
