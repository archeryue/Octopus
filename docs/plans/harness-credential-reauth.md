# Harness credential re-authorization (reactive 401 detection)

## 1. Problem

A harness sign-in credential (Claude Code OAuth / API key, Codex
device-auth) can become invalid *while a session is running* — the token
is revoked, an API key is rotated, or an OAuth token expires in a way the
**proactive** refresh path (`_refresh_oauth_if_needed`) never caught
(e.g. a pasted API key has no `token_expires_at`, so nothing pre-checks
it). When that happens the CLI fails the turn with an auth error, e.g.:

```
Failed to authenticate. API Error: 401 Invalid authentication credentials
```

Today that surfaces as a generic red error bubble (or, for some shapes,
is dropped entirely) and the bound credential is **never** flagged. The
user gets the same failure every turn with no affordance to fix it.

Connectors already solve the analogous problem: a refresh failure sets
`needs_reconnect=True` + `last_refresh_error_code` on the installation,
the sidebar shows a "reconnect" button, and re-auth clears the flag
in place. This plan brings the **reactive** (mid-turn 401) equivalent to
harness credentials, reusing the credential schema fields that already
exist for the proactive path (`needs_reconnect`, `last_refresh_error_code`,
`status`).

## 2. Scope

In: detect a mid-turn auth-credential rejection, mark the bound
credential, surface it (chat error + sidebar badge), and let the user
re-authorize **in place** (same credential id, so all agent/session
bindings survive) which clears the flag.

## 3. Detection lives behind the harness contract

Per the harness-layer rule (no `if backend ==` outside `server/harness/`),
each backend declares how *its* CLI reports an auth-credential rejection:

- `RuntimeProfile.auth_error_patterns: tuple[str, ...]` — lowercased
  substrings. A turn-failure whose combined error text contains any of
  them is an auth-expiry.
- `Harness.is_auth_error(text) -> bool` — case-insensitive substring match
  over the profile patterns. Pure; no I/O.

Patterns are intentionally specific phrases ("invalid authentication
credentials", "invalid x-api-key", "401 unauthorized", "invalid_grant",
…) rather than a bare `401`, and detection is **gated on the turn having
failed** (see §4), so a tool that merely *returns* a 401 to the model
(which then continues to a normal result) never trips it.

## 4. Run-loop wiring (`session_manager._run_backend`)

The combined error text for a turn is the terminal error event's
`content` (Codex `turn.failed`/`error` carry the message) plus the run's
captured `stderr_text` (Claude prints API errors there). After the stream
ends and `backend.stop()` runs:

- A turn is *failed* when it produced an `is_error` `result`/`error`
  event, or ended with no `result` at all.
- If failed AND `harness.is_auth_error(blob)`:
  - mark the bound credential (`session.credential_id` ?? agent's)
    `needs_reconnect` with `RefreshErrorCode.invalid_credentials` (reuses
    `_mark_needs_reconnect`); no-op if no credential is bound (host-default
    auth);
  - persist + broadcast a tagged error event
    `{type:"error", code:"auth_expired", credential_id, backend, message}`;
  - return immediately — the Claude premature-exit "continue" respawn must
    NOT run (re-auth won't fix itself, and retrying just burns the budget).

Detection reads the raw `HarnessEvent` stream directly (terminal event
`content`/`raw` captured in the loop), not the WS/persistence mappers — so
it works even for `type="error"` events that those mappers drop today, with
no change to result/error rendering semantics and no duplicate bubbles.

## 5. Re-authorize in place + clear-on-success

The durable affordance mirrors connectors: the sidebar credential shows a
"needs reconnect" badge + "Re-authorize" button. Re-auth re-runs the same
login flow but targets the **existing** credential id, so bindings stay
valid and the flag clears.

- **Claude** (`POST /credentials/oauth/complete`): optional
  `credential_id`. When set, `update_credential(secret, token_expires_at,
  needs_reconnect=False, last_refresh_error_code=None, status=active,
  label)` in place instead of inserting a new row.
- **Codex** (`POST /credentials/codex/start`): optional
  `reauth_credential_id`. The login re-runs `codex login` with
  `CODEX_HOME = codex_home_for(reauth_credential_id)` — i.e. it overwrites
  `auth.json` in the *same* directory the credential already points at, so
  no path changes and no dir cleanup is needed. The status-route persist
  branches: credential row exists → update + clear flag; else insert (new
  sign-in).
- **API key** (`PATCH /credentials/{id}` with a new secret): clears the
  flag too, so re-pasting a fresh key recovers an `api_key` credential.

A brand-new login already creates `status=active`, so no change there.

## 6. Frontend

- `CredentialList.tsx`: per-credential `needs_reconnect` → amber/destructive
  "needs reauth" badge + a "Re-authorize" button that opens the existing
  dialog in re-auth mode, threading `credential_id` (Claude) /
  `reauth_credential_id` (Codex). On success, refetch → flag clears.
- `useWebSocket.ts`: an `error` event with `code === "auth_expired"` renders
  the chat error text (as today) AND refetches `/api/credentials` so the
  sidebar badge appears immediately without a reload.

## 7. Test gating (real-CLI suites)

Surfacing this bug in the app also exposed it in the test suite: the
`tests/*_real.py` gates skipped only on a missing binary, so an *expired*
host login (the `claude` CLI present but logged out) ran them and turned
them red with the very 401 this feature detects — noise that masquerades
as a product regression. The gates now call shared helpers in `tests/cli_gate.py`
(`claude_cli_works()` / `codex_cli_works()`, imported as
`from tests.cli_gate import …` — `import conftest` isn't reliable under
pytest collection) that confirm the CLI is present **and** signed in (claude
via a tiny cached real call; codex via `~/.codex/auth.json`), so a logged-out
environment SKIPS rather than fails.
`test_codex_login_real.py` still gates on the binary alone — it tests the
sign-in flow itself and must run while logged out.

## 8. What this defers

- No automatic *retry* of the failed turn after re-auth — the user re-runs
  their prompt. (Connectors don't auto-retry either.)
- No change to the proactive OAuth-refresh path; this is purely the
  reactive complement.
