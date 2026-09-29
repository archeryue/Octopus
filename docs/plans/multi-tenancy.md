# Multi-tenancy (`user-1.0`)

> **Status:** planned — one site, many users: the label becomes a username, the token becomes a password, and every user gets their own directory, rows and keys.

Octopus is single-user by construction: one `OCTOPUS_AUTH_TOKEN` is the API
credential, the WebSocket ticket, the application cookie, the MCP signing key
*and* the key every stored secret is encrypted with. `user-1.0` turns that into
accounts — and does it in the shape the eventual cloud version wants, without
building the cloud version's isolation on a single box.

---

## 1. What this is, and what it is not

**It is**: accounts, per-user data, per-user directories, per-user keys, and a
site several people can sign into.

**It is not a security boundary between tenants.** That is a deliberate,
recorded decision (§2), not an oversight. Anyone with an account can run an
agent, an agent can run shell commands, and every agent runs as the same OS
user — so any tenant can read any other tenant's files, including their CLI
credentials and the database itself. `user-1.0` is for people who already trust
each other; the trust boundary arrives with per-tenant containers in the cloud
version.

**It is not a team product**, and that is a product decision rather than a
roadmap item. Octopus is a personal agent: multi-tenancy here means several
*private* installs behind one site, never a shared workspace. There are no
teams, no shared agents, no collaboration surface, and nothing below is shaped
to leave room for one.

That absence is load-bearing, not a gap. The questions that make sharing hard —
who owns a session two people touched, whose credential runs it, whose quota it
spends, who may read its transcript — simply never arise, which is why §5 can
own everything by a single `user_id` and §8 can count quota per person without
qualification.

This is all stated first because every other decision below is only defensible
given it.

---

## 2. Isolation: the lowest rung, on purpose

| | Approach | Isolation | Decision |
|---|---|---|---|
| A | One OS user, directory convention | none between tenants | **chosen** |
| B | One OS user *per tenant*, `runuser` to spawn | POSIX permissions + cgroup quotas | not now |
| C | Container or VM per tenant | real | the cloud version |

The case for A over B here is not that A is adequate — it isn't — but that the
cloud version replaces the execution layer wholesale. Building B means designing
a privilege-dropping spawn path, a per-tenant home layout and a setuid helper
that C then deletes. The work that survives the move to C is the work A already
forces: per-user data, per-user directories, per-user keys, scoped fan-out.

What A costs us is written down and not smoothed over: **an invite code gates
who gets an account, not what an account can reach.**

---

## 3. Identity

```
users          id · username · password_hash(argon2id) · dek_wrapped
               · is_admin · disabled_at · created_at · extra_roots(JSON)
invites        code · created_by · expires_at · max_uses · used_count
auth_tokens    token · user_id · kind('session'|'pat') · label
               · expires_at · last_seen_at · revoked_at
```

* `username` is today's `OCTOPUS_USER_LABEL` — the sidebar already reads it
  from `GET /api/auth/identity`, so that surface needs a value change, not a
  redesign.
* `POST /api/auth/login` returns a **session token** stored in `auth_tokens`.
  The password is never a bearer.
* **Opaque tokens, not JWTs.** Disabling a user, revoking a device and changing
  a password must take effect immediately; with a JWT that means either short
  expiry plus refresh or a revocation list, and neither is worth the complexity
  next to a table lookup in a SQLite file that is already open.
* **Registration is by invite code.** Admin creates a code, registration
  consumes it. No open signup, no email flow, no password reset in `user-1.0`
  (an admin sets a new password; see §11).
* **Login throttling is not optional.** Today's credential is a high-entropy
  token; a human-chosen password is not, so per-username and per-IP backoff
  ships with the login route, not after it.

### 3.1 The fate of `OCTOPUS_AUTH_TOKEN`

It is **retired**, and its five jobs are split:

| Job today | Becomes |
|---|---|
| API credential | per-user session token |
| WebSocket `?token=` | same session token |
| `octopus_app_token` cookie | per-user, per-app scope token derived from the master key |
| MCP bearer signing key | `OCTOPUS_MASTER_KEY` |
| Encryption key for every stored secret | per-user DEK, wrapped by the master key (§4) |

Scripts and the CLI keep working through a **personal access token** — the same
`auth_tokens` table with `kind='pat'`, revocable, no expiry, created from the
account page. Keeping the old global token as a "machine token" was considered
and rejected: it is a second authentication path that outranks every account,
must be maintained forever, and is exactly what the cloud version would have to
remove again.

---

## 4. Keys

The single most important correction to "the token becomes the password": the
token has a second job the password **must not** inherit.

> A schedule fires at 07:00 while its owner is asleep and not signed in. The
> server has to decrypt that user's Claude credential to run the turn. A key
> derived from their password does not exist at that moment.

So:

```
OCTOPUS_MASTER_KEY            (server-held: file 0600, or KMS in the cloud)
   └── wraps users.dek_wrapped   (one random DEK per user)
          └── encrypts that user's credentials and connector tokens

password → authentication only. Changing it re-encrypts nothing.
```

Consequences worth stating plainly:

* `docs/plans/token-rotation.md`'s whole re-keying operation **disappears** from
  the model. Rotating a server key is an ops action over wrapped DEKs, not a
  rewrite of every ciphertext.
* The server, and root on the box, can decrypt every user's secrets. Unavoidable
  while the server runs CLIs on behalf of offline users. "The server cannot read
  it" requires per-tenant execution *and* the user present, which is a different
  product.
* Deleting a user is a crypto-shred: drop the DEK and their ciphertexts are
  noise, whatever else is still on disk.

---

## 5. Data ownership: one database, `user_id`, scoped at one layer

**Decision: a single SQLite database with `user_id` on the root tables.**

Per-user databases were the alternative, and their structural argument is real —
a forgotten `WHERE` cannot leak what a connection cannot see. It loses on three
counts here:

1. A control-plane database is needed regardless (users, invites, tokens,
   application port allocation), so per-user means *1 + N*, not *N*.
2. The migration ledger (`polish-2026-09.md` §3 A3) runs per database. *N* of
   them means *N* migration runs and a new failure mode — the half-migrated
   estate — on every upgrade.
3. Under isolation level A the forgotten-`WHERE` risk is **not the weakest
   link**: an agent can read the database file directly. Paying a structural
   cost for a guard that the threat model already walks around is the wrong
   trade.

**Root tables gaining `user_id`** (7): `agents`, `applications`,
`backend_credentials`, `connector_installations`, `custom_connectors`,
`connector_oauth_clients`, `notifiers`.

**Derived tables do not**, and inherit ownership through their foreign key:
`messages`→sessions, `schedules`/`agent_connectors`→agents,
`bg_tasks`/`research_jobs`→sessions, every `*_secrets`→its parent.

**`sessions` breaks that rule and carries `user_id` too.** Its `agent_id` is
nullable (orphan sessions exist), and the WebSocket fan-out has to route by
owner on every frame — one denormalised column beats a join on the hottest path
in the product.

**Every edge in the graph stays inside one user.** An agent delegates only to
its owner's agents (`mcp__ask_agent__ask` resolves the target name within the
caller's set, so another user's "Vera" is simply not found); a delegation
child's parent is the same user's session; a fork's parent likewise. The cycle
and depth-3 guards already walk `parent_session_id`, so the ownership check
rides the walk they already do. This is not a restriction awaiting relaxation —
per §1 there is no sharing to relax it into.

### 5.1 Scoping is structural, not per-route

There are ~101 routes and 119 `verify_token` call sites. Adding
`WHERE user_id = ?` to each of them is how multi-tenant systems leak: it only
has to be forgotten once, by anyone, ever.

Instead the scoping lives in one layer, reusing what `§3 A2` already built:
`server/deps.py` gains a `Ctx` dependency carrying the current user and a
**pre-scoped** manager, so a route *cannot* reach another user's rows without
deliberately asking for an unscoped accessor. `SessionManager` holds its
sessions partitioned by user rather than in one flat map, so the same guarantee
holds for the in-memory half.

This layer is also what makes the cloud move cheap: when a tenant becomes a
database (or a container), only `Ctx` changes.

---

## 6. Files and the workspace

```
<root>/users/<user_id>/
    workspace/      ← every session's working_dir lives under here
    agents/         ← per-agent memory (agent_memory.py)
    applications/   ← <slug>/ <slug>.data/ <slug>.runtime/
    attachments/  large-prompts/  codex/  fork/
```

The eight `settings.*_dir` constants become functions of a user.

**`working_dir` is confined to the workspace.** Today it is
`Path(raw).expanduser().resolve()` with no boundary at all — any absolute path
on the box. It must resolve (symlinks included, or the check is theatre) inside
the owner's workspace, or the request is refused.

**`users.extra_roots`** is an admin-set list of additional permitted prefixes.
It exists for exactly one reason: the first user develops Octopus itself at a
path outside any workspace, and moving that repository is not something this
change should force. It is marked here as **a single-box affordance the cloud
version drops** — in the cloud a tenant's workspace is their whole world, and
the column becomes uniformly empty.

---

## 7. Fan-out and the tool surface

Three places assume one user and would cross tenants silently:

1. **`SessionManager._broadcast` is a global fan-out** — every registered
   callback receives every frame. Left alone it would stream one user's
   `assistant_text` into another user's browser. Callbacks get grouped by user
   and frames routed by `sessions.user_id`.
2. **`mcp_identity`'s scope** widens from `session_id` to
   `(user_id, session_id)`, signed with the master key rather than the user's
   credential — otherwise changing a password would break every tool call in a
   turn that is currently running.
3. **Application tokens** derive from the master key and carry the owner;
   `/apps/{id}` checks ownership. An unguessable id is not authorisation.

The in-process MCP design (`polish-2026-09.md` §4 B1) needs no rework: one
shared mount plus identity in the call's verified scope is already the
multi-tenant shape. Only the scope's contents grow.

---

## 8. Scheduling, quotas, monitoring

* One APScheduler process runs everyone's jobs; a job carries its `user_id` and
  builds that user's context (directories, credential, DEK) when it fires.
* **Quotas are a feature, not a refinement.** A held `claude` is ~250 MB
  (`inline-steering.md` §7 records the OOM this caused with one user). Per-user
  concurrent-turn caps and a global cap ship with this version, and
  `stop_all_held_processes` becomes per-user.
* The monitor splits: box-level gauges (PSS, sidecar count) are admin-only;
  per-user usage (turns, errors, tokens) is visible to the owner.

---

## 9. Migrating the existing install

One install exists, with ~49,500 messages, live credentials and six schedules.
It becomes user #1 with no data loss:

1. Create `users` / `invites` / `auth_tokens`; insert user #1 with
   `username = OCTOPUS_USER_LABEL` and a password set during the upgrade.
2. Generate `OCTOPUS_MASTER_KEY`, mint user #1's DEK, wrap it.
3. **Re-encrypt every secret** from `PBKDF2(old OCTOPUS_AUTH_TOKEN)` to the new
   DEK. This is not new machinery: `server/token_rotation.py` already
   re-encrypts all three secret tables in one transaction with rollback on any
   row that fails to decrypt. The upgrade calls it with a new key derivation
   rather than inventing a second path.
4. Backfill `user_id = 1` on the seven root tables and `sessions`.
5. Move the existing directories under `users/1/`, and set that user's
   `extra_roots` to the paths their current sessions already use, so no live
   session breaks.
6. Retire `OCTOPUS_AUTH_TOKEN` from the env files.

It is a one-way door on a live box, so it is rehearsed against a copy of the
production database first — the way `A3` was.

---

## 10. Order of work

1. **Control plane** — the three tables, login/registration/invites, `Ctx` in
   `deps.py`, session tokens replacing the global token end to end (API, WS,
   app cookie). Nothing is multi-user yet; everything is *shaped* for it.
2. **Keys** — master key, per-user DEK, the re-encryption path from §9.3.
   Before the data backfill, because the backfill assumes a user exists.
3. **Data ownership** — `user_id` columns, backfill, scoped accessors, the
   partitioned `SessionManager`. The largest mechanical chunk.
4. **Filesystem** — per-user roots, workspace confinement, `extra_roots`.
5. **Fan-out** — scoped broadcast, MCP scope, application tokens.
6. **Quotas and the admin surface** — caps, user create/disable, invite codes.
7. **Migrate the live install** (§9), then the docs and `CLAUDE.md`.

Steps 1–2 are the spine; 3–5 can be reviewed independently; 6 is small; 7 is
the only irreversible one.

---

## 11. What this defers

* **Real isolation between tenants** — per-tenant containers, an execution
  sandbox, network policy. This is the cloud version, and it replaces §2's
  choice rather than extending it.
* **SSO / OAuth sign-in, email verification, self-service password reset.**
  An invite code and an admin who can set a password cover a small trusted
  group; none of the rest is worth building twice.
* **Billing and usage accounting** beyond the per-user quotas in §8.
* **Per-user databases.** Reconsidered when tenants move to their own
  containers, where the export script from §5 is the migration anyway.
* **Audit logging.** Cheap to add later and valueless without a trust boundary
  to audit against.
