# Connectors under multi-tenancy: one call, one owner, one installation

> **Status:** in-progress — Proposal: make every connector call carry its own identity, so no account can reach another's mailbox, and close the design gaps around it.

> **Implementation status: PROPOSED (2026-10-04), nothing built.** Written from a
> read-only review of `origin/main` at `6d6ba0b` (the deployed user-1.0 build)
> plus read-only counts from the deployed database. Google's "Testing" publishing
> status (test-user list, 7-day refresh tokens, restricted-scope verification) is
> **out of scope** by decision — it is a publishing step for when there are more
> users, not a tenancy problem.

Related: [`connectors.md`](connectors.md) (the original framework),
[`multi-tenancy.md`](multi-tenancy.md) (accounts, keys, scoping),
[`polish-2026-09.md`](polish-2026-09.md) §4 B1 (in-process MCP),
[`token-rotation.md`](token-rotation.md) §6.

---

## 1. What is already right

Multi-tenancy reached the connector *data* correctly. This plan keeps all of it:

* **Two halves, correctly split.** The connector *kind* and its OAuth client are
  install-level (`connector_oauth_clients`, `custom_connectors` — no owner
  column, secret under the master key, configured by the operator). The
  connected *account* is per-user (`connector_installations.user_id`, token in
  `connector_installation_secrets` under that user's DEK).
* **Every REST read and write is owner-scoped.** `get_connector_installation(id,
  user_id)` returns `None` for "someone else's" exactly as for "missing", so
  every route 404s across accounts.
* **The dedup index is per user** — `(COALESCE(user_id,''), kind,
  external_account_id)` after the `_make_index_per_user` migration — so two
  accounts connecting the same Gmail each get their own installation.
* **The OAuth flow remembers its initiator.** `oauth/start` (authenticated)
  records `user_id` on the pending login; status and cancel check it.
* **Once an account exists, the install token opens nothing** (`auth._allowed`),
  so every request resolves to an owner. The scope is never silently "all".
* **A turn's tool calls act as the session's owner.** The MCP bearer is signed
  with the master key and names `(session, installation, user)`; the `/token`
  route checks the installation belongs to that user.

## 2. Facts from the deployed install (read-only, 2026-10-04)

| | |
|---|---|
| Accounts | 4 (2 disabled) |
| Gmail installations | **2, owned by 2 different accounts** |
| GitHub installations | 1 |
| Custom connector kinds | 0 |
| Agent ↔ installation links | 8, **0** crossing owners |
| Disabled accounts' installations / enabled schedules | 0 / 0 |

The second row is why §3 P1 is live today rather than theoretical.

---

## 3. Problems

Ordered by severity. Each was confirmed in code; P1 was also reproduced.

### P1 — The token cache is shared by every installation of a kind 🔴 live

`server/mcp_servers/connectors/gmail.py:41` (and `github.py:42`, `custom.py:40`)
creates one module-level `ctx = ConnectorContext()`. Its cache
(`_shared.py:67`) is two bare fields, `_access_token` / `_token_exp`, with no
installation in the key. Since B1 every kind is served from **one** in-process
mount (`/mcp/gmail`) for every session of every account, and tool bodies run
concurrently on anyio's worker threads (`mcp_http.py:82`, limit 40).

So: Archer's agent calls a Gmail tool → `ctx` caches Archer's access token for
~1 h. Within that hour Vera's agent calls a Gmail tool — its scope correctly
names Vera's installation, but `access_token()` sees a fresh cached token and
returns **Archer's**. Google answers 200 because the token is valid, so nothing
fails: Vera's agent searches, reads and — via `send_draft` — **sends from
Archer's mailbox**, silently.

Reproduced with the real `ConnectorContext` driven through two installations:
the second call received the first installation's token and never fetched its
own. Also affected: one account with two Gmail accounts (wrong mailbox), GitHub,
and custom connectors — where two *different* custom kinds share the module, so
one service's token is sent to another service's API.

No test covers two installations; every connector test uses one, which is why
the suite is green.

**Root cause, which matters more than the instance.** The connector MCP servers
were written for *one process per installation* (stdio sidecars): identity came
from the process environment, so a module global *was* per-installation by
construction. B1 moved them to one shared mount and moved the *installation id*
into the call's verified scope (`_host.resolve`) — but not the state that had
been per-process implicitly. Multi-tenancy then layered `user_id` on top. Any
module-level state in a connector server is now shared by everyone; the fix has
to make that impossible, not just patch this cache.

### P2 — Custom connectors are broken in-process, and can leak their token 🔴 latent

* `custom.py`'s `_api_base()` reads `os.environ["OCTOPUS_CONNECTOR_API_BASE"]`
  directly — not through `_host.resolve`. Only the stdio `mcp_entry`
  (`server/connectors/custom.py:165`) ever set it, and production always takes
  the HTTP path (`harness/run.py:245` passes `session_id`). So every custom
  connector call answers *"connector misconfigured (no API base)"*. Latent only
  because the deployed install has no custom kinds.
* `request` accepts an absolute URL: `url = path if path.startswith("http")
  else …`. The installation's OAuth token is attached to **any host** the model
  names. A prompt injection in fetched content can exfiltrate it.

### P3 — The OAuth callback is not bound to the browser that started it 🟠

`oauth_callback` (`routers/connectors.py:252`) trusts the pending login's
`user_id` and checks only the CSRF `state`. Nothing ties the callback to the
browser that called `oauth/start`. An attacker can start a login in their own
account and send the resulting Google consent URL to a victim; if the victim
consents, **the victim's Gmail is installed into the attacker's account**. It
needs the victim to click "Allow" on a Google screen, so the risk in a small
trusted group is low, but it is a real hole in the trust model.

### P4 — Ownership is enforced when a link is written, never when it is used 🟡

`set_agent_connector` / `replace_agent_connectors` check that the installation
belongs to the caller. But the turn-time loader
(`db/agents.py:52 get_enabled_connectors_for_agent`, called from
`sessions/turns.py:1362`) joins on `agent_id` alone. The deployed data has 0
cross-owner links, and `/token` would still refuse a foreign installation — so
this is not a leak today, but a cross-owner link (pre-accounts data, a future
bug, a hand edit) would put another account's name and tools into an agent's
system prompt. The read path should not depend on the write path having always
been right.

### P5 — A disabled account keeps using its connectors 🟡 latent

`UserManager.set_disabled` revokes login tokens only. `disabled_at` is checked
nowhere else: the scheduler still fires the owner's schedules, `verify_mcp_scope`
still accepts their turns' bearers, and `get_access_token` still refreshes and
hands out their Gmail token. Latent today (the 2 disabled accounts have no
installations or enabled schedules).

### P6 — Disconnecting does not revoke anything at the provider 🟡

`delete_installation` drops the local rows. The refresh token stays valid at
Google (and the GitHub grant stays authorised) until it expires or the user
removes it by hand in their Google account. "Disconnect" in the UI implies
Octopus can no longer reach the mailbox; right now that is only true of this
database.

### P7 — Refresh failures are all treated as revocation 🟡

`get_access_token` catches **any** exception from `provider.refresh` and flips
`needs_reconnect`: a DNS blip, a timeout or a Google 503 looks identical to
`invalid_grant`, and the user is told to reconnect an account that is fine. This
path also records no monitor event (only the `/mark-needs-reconnect` route does).
In the other direction, a Gmail API **401** in a tool marks the installation
broken immediately without first trying a refresh.

### P8 — Dead and misleading code 🟢

* The stdio connector path (`ConnectorBase.mcp_entry`, `callback_env`,
  `OCTOPUS_INSTALLATION_ID` / `OCTOPUS_CONNECTOR_API_BASE` in env) has no
  production caller; `select_mcp_servers` without `session_id` is reached only
  from tests. Its presence is what made P2 look plausible to a reader.
* `enable_by_default` is stored, serialised and typed, and never applied (no
  agent creation path reads it; no UI sets it).
* Docstrings that describe the pre-tenancy world: `routers/connectors.py` says
  *"Installations are global"*; `connector_manager.py` and `db/connectors.py`
  refer to a *"boot-time refresh sweep"* that does not exist.

### P9 — Token rotation still re-keys connector secrets with the token 🟢 known

Recorded in `multi-tenancy.md` §11 / `token-rotation.md` §6: rotation tries to
re-key every `connector_installation_secrets` row with the access token, but
account rows are under per-user DEKs, so `POST /api/auth/rotate` fails (safely)
whenever any account has connected anything.

---

## 4. Design

### 4.1 The principle

> **A connector tool call's identity is `(user_id, session_id, installation_id)`
> from its verified scope, and nothing in a connector server holds state that
> outlives one call.**

Everything a call needs — which installation, whose token, which API base — is
resolved from the scope at the time of the call, on the server side, behind the
owner check. The tool module keeps no cache, no globals beyond `mcp` itself, and
reads no process environment.

### 4.2 Tokens: the server owns the cache, the tool owns nothing (P1)

* **Delete the client-side cache.** `ConnectorContext` becomes a stateless
  helper: every property resolves from the call's scope, and `access_token()`
  asks the host every time.
* **Keep the loopback `/token` route** as the one authorisation point — it
  already checks `installation.user_id == scope.user_id`. Its cost (~1–2 ms on
  loopback) is noise beside a Gmail API round trip, and it keeps tool bodies off
  the event loop (they run on worker threads, `mcp_http.py:82`).
* **Cache, if anywhere, inside `ConnectorManager`**, keyed by `installation_id`,
  populated only *after* the owner check, invalidated on refresh, reconnect,
  delete and owner-disable. It saves a decrypt, not a network call, so it is
  optional; correctness must not depend on it. Recommendation: no cache in v1 of
  this change — decrypting a small blob per call is cheap and leaves nothing to
  get wrong.
* **Make the bug class structurally impossible**, not just this instance: a
  test imports every module under `server/mcp_servers/connectors/` and fails if
  any module-level object other than `mcp` holds mutable per-call state (in
  practice: no `ConnectorContext` instance at module level, no module `dict`
  / `list` caches). The same rule goes into the `connectors.md` "how to add a
  connector" guidance.

### 4.3 Custom connectors resolve their API from the installation (P2)

* The `/token` response grows a non-secret `api_base` (the kind's
  `custom_connectors.api_base`, `null` for built-ins), so the custom server gets
  the base from the same owner-checked lookup as the token. `_api_base()` and
  every `os.environ` read in connector servers are removed.
* **Requests are confined to the API base.** `path` must be relative; the joined
  URL is parsed and refused unless its scheme, host and port equal the base's
  and its path stays under the base's path (after normalising `..`). An absolute
  URL in `path` is an error returned to the model, never a request.

### 4.4 Bind the OAuth callback to the browser that started it (P3)

* `oauth/start` additionally returns a `Set-Cookie: octopus_oauth_bind=<random>;
  HttpOnly; Secure (when https); SameSite=Lax; Path=/api/connectors/oauth;
  Max-Age=900`, and stores only a hash of that value on the `PendingLogin`.
  (The SPA calls `start` same-origin with `fetch`, so the browser stores the
  cookie; `credentials: "same-origin"` must be set on that call.)
* `oauth/callback` requires the cookie and compares hashes in constant time
  before exchanging the code. Missing or mismatched → *"This sign-in was
  started in a different browser"*, and the pending login is marked error.
  `SameSite=Lax` is exactly right here: Google's redirect back is a top-level
  GET navigation, which carries Lax cookies; a cross-site subrequest does not.
* Cost, stated plainly: a flow started on the laptop cannot be finished on the
  phone. That is the property we want.

### 4.5 Ownership checked where it is used (P4)

`get_enabled_connectors_for_agent` joins `agents` and adds
`ci.user_id IS a.user_id` (null-safe, for the pre-accounts install), so the
loader can only ever return the agent owner's installations. `/token` keeps its
own check. A test inserts a cross-owner link directly into the DB and asserts
the turn sees neither its blurb nor its tools.

### 4.6 A disabled account is off everywhere (P5)

One predicate, `UserManager.is_active(user_id)`, consulted at the three places a
disabled account can still act without signing in:

1. **Scheduler fire** — skip, and record a `schedule_skipped` monitor event with
   reason `owner_disabled` (the overlap guard's skip path already exists).
2. **MCP scope verification** — `verify_mcp_scope` stays a pure signature check;
   `auth._allowed` / `scope_user_id` additionally reject a scope whose user is
   disabled. This also stops a turn that was mid-flight when the account was
   disabled at its next tool call.
3. **`get_access_token`** — refuses for a disabled owner (defence in depth).

And `set_disabled(True)` stops the account's held CLI processes
(`stop_all_held_processes` is already per-user, `multi-tenancy.md` §8).
Re-enabling restores everything; nothing is deleted.

### 4.7 Disconnect means disconnected (P6)

* `ConnectorOAuthProvider` gains an optional `revoke(*, client_id,
  client_secret, token_set)`. Gmail: `POST https://oauth2.googleapis.com/revoke`
  with the refresh token (revoking it invalidates its access tokens). GitHub:
  `DELETE /applications/{client_id}/grant` with basic auth. Custom kinds: an
  optional `revoke_url` on the definition; absent → no provider call.
* `delete_installation` revokes first, then deletes. A revoke failure does
  **not** block the delete (the user asked to disconnect; leaving our copy would
  be worse), but it is recorded and reported in the response
  (`revoked: false, detail: …`) so the UI can say "also remove access in your
  Google account".

### 4.8 Refresh that tells transient from revoked (P7)

* `provider.refresh` failures are classified: token-endpoint `400/401` with
  `invalid_grant` / `invalid_client` / `unauthorized_client` → **revoked**
  (`needs_reconnect = true`); network errors, timeouts, `429` and `5xx` →
  **transient** (`needs_reconnect` unchanged; the call fails with a retryable
  message and `last_refresh_error_code` records it).
* Both outcomes record a `connector_refresh` monitor event, so the operator view
  sees flapping as well as breakage.
* A provider-API `401` inside a tool triggers one forced refresh
  (`GET /token?force_refresh=1`) and one retry before the installation is marked
  `needs_reconnect`.

### 4.9 Remove what nothing uses (P8)

* Delete the stdio connector path: `ConnectorBase.mcp_entry`, the
  `OCTOPUS_INSTALLATION_ID` / `OCTOPUS_CONNECTOR_API_BASE` env plumbing, and the
  connector half of `select_mcp_servers`' stdio fallback. Tests that inspect
  argv render the HTTP form with a test session id.
* `enable_by_default`: **remove** (column, model field, contract) rather than
  implement. Auto-attaching a connected mailbox to every new agent is a
  surprising default for the thing with `send_draft`; enabling stays an explicit
  per-agent choice. (If it is wanted later, it is a per-account preference, not
  a per-installation column.)
* Fix the three misleading docstrings to describe the per-account world.

### 4.10 Token rotation scoped to install-era secrets (P9)

As `token-rotation.md` §6 already prescribes: rotation re-keys only rows with
`user_id IS NULL`; DEK-keyed rows are left alone. Included here because
connector secrets are half of what it trips on, and the fix is small.

---

## 5. Data and API changes

| Change | Kind |
|---|---|
| `connector_installations.enable_by_default` | drop column (migration, guarded on presence) |
| `custom_connectors.revoke_url` | add, nullable |
| `PendingLogin.bind_hash` | in-memory field |
| `GET /api/connectors/{id}/token` | response adds `api_base`; accepts `force_refresh` |
| `POST /api/connectors/oauth/start` | sets the bind cookie |
| `GET /api/connectors/oauth/callback` | requires the bind cookie |
| `DELETE /api/connectors/{id}` | revokes at provider; returns `{revoked, detail}` |
| `ConnectorOAuthProvider.revoke` | new optional method |

No change to the ownership model, the keys, or the tables' owners.

---

## 6. Verification

Unit / hermetic:

* **Two installations, alternating** — the P1 regression: installations of two
  accounts, and two of one account, called A, B, A, B through the real mount;
  each call presents its own token. Repeated for GitHub and custom.
* **No module-level state** in any connector server (the structural test, §4.2).
* Custom: in-process call resolves `api_base` from the installation; absolute
  URL, other host, `..` escape, and scheme change are all refused.
* OAuth callback without the bind cookie, and with another login's cookie, is
  refused; with its own it succeeds.
* A cross-owner `agent_connectors` row is invisible to the turn.
* Disabled owner: schedule skipped with the event, scope rejected, `/token`
  refused, held processes stopped; re-enable restores.
* Delete calls `revoke` once; a revoke failure still deletes and reports it.
* Refresh: `invalid_grant` flips `needs_reconnect`; timeout and 503 do not;
  both record events; a tool 401 forces exactly one refresh before flagging.
* Rotation succeeds with account-owned connector secrets present.
* `tests/test_tenant_isolation.py` gains: "Vera's agent calling Gmail reaches
  Vera's mailbox" end to end.

Against the real thing:

* **Rehearsal on a snapshot of the deployed database** (the multi-tenancy
  rehearsal procedure, `multi-tenancy.md` §10), with both real Gmail
  installations: plant a uniquely-subjected message in each mailbox, then have
  each account's agent `search` for both subjects, alternating, inside one
  token lifetime — each must only ever find its own. Using the copied tokens is
  safe here in a way the general procedure is wary of: Google does not rotate a
  refresh token when it is used, and GitHub OAuth tokens never refresh, so the
  copy cannot invalidate the live install's tokens. They are still cleared
  afterwards, as that procedure requires.
* Real-CLI tier: one `real_claude` case where two accounts' agents use Gmail in
  overlapping turns.
* e2e: the bind-cookie flow through the Connectors page (start → consent stub →
  callback), and disconnect showing the revoke result.

---

## 7. Order of work

1. **P1 + P2 first, as one change** — the live cross-tenant leak and the
   latent one share the root cause and the fix (§4.1–4.3). Ship it on its own,
   rehearsed against the snapshot, before anything else. Until it lands, the
   safe operational stopgap is to keep Gmail enabled on agents of only one
   account.
2. **P3** — the callback binding (small, self-contained).
3. **P4 + P5** — ownership at use, disabled means off.
4. **P6 + P7** — revoke on disconnect, refresh classification.
5. **P8 + P9** — removals and rotation scoping.

Each step passes the full suite (hermetic, real-CLI, e2e) on its own.

---

## 8. What this defers

* **Google publishing** — moving the OAuth app out of "Testing" (test-user list,
  7-day refresh tokens, `gmail.modify` restricted-scope verification / CASA).
  Deferred by decision until there are more users.
* **Account deletion and crypto-shred.** `multi-tenancy.md` §4 describes
  deletion as dropping the DEK, but no delete-account path exists. When it is
  built, it calls §4.7's revoke for each installation first. It needs a product
  decision (who may delete, what happens to shared-nothing data), so it is not
  invented here.
* **Persisting pending OAuth logins.** They live in memory with a 15-minute TTL;
  a restart mid-consent fails that one attempt and the user starts again. Worth
  persisting only with more than one server process.
* **Per-account connector quotas.** Google's API quota is per OAuth client, so
  one account can exhaust it for all. Not a problem at four accounts; revisit
  with the publishing work.
