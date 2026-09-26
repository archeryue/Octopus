# Rotating the access token without breaking anything

> **Status:** shipped — Rotating `OCTOPUS_AUTH_TOKEN`: re-key every stored secret, rewrite the env files, and keep open tabs working.

> **Implementation status: SHIPPED.** `server/token_rotation.py`, the
> `/api/auth/rotate` route, the Change-token panel in Settings, and the two
> client paths (`auth_token_rotated`, and a WebSocket closed with `4001`).
> Tests: `tests/test_token_rotation.py` (15 — every refusal, the all-or-nothing
> re-encryption, the env rewrite, and the broadcast in both modes),
> `SettingsDialog.test.tsx`, `useWebSocket.test.ts`.

## 1. Why this needs a feature at all

`OCTOPUS_AUTH_TOKEN` is not just the password on the door. `server/crypto.py`
derives a Fernet key from it (PBKDF2), and that key encrypts everything
secret in the database:

| table | column | what it holds |
|---|---|---|
| `credential_secrets` | `secret_encrypted` | an agent's API key or OAuth bundle |
| `connector_installation_secrets` | `secret_encrypted` | GitHub / Gmail tokens |
| `connector_oauth_clients` | `client_secret_encrypted` | OAuth client secrets |

So "change the token" is really three operations that have to agree: rewrite
those ciphertexts, change what the server checks requests against, and change
what every signed-in client sends. Doing them by hand — edit `.env`, restart —
performs the middle one and silently breaks the other two: credentials stop
decrypting (the session logs `Could not decrypt credential … (wrong auth
token?)` and quietly falls back to the host CLI's own login), and every open
tab spins on a WebSocket that closes with `4001` forever, because nothing
tells it to ask for a new token.

## 2. One operation, server-side

`POST /api/auth/rotate` does all of it, in an order chosen so a failure
anywhere leaves the system consistent:

1. **Validate** the new token: not empty, not the default, not the current
   one, long enough, and free of characters that break the places a token
   travels — it rides in a URL query (`/ws?token=`) and a cookie
   (`octopus_app_token`), so whitespace, `;` and `,` are refused.
2. **Write the env files** that actually define `OCTOPUS_AUTH_TOKEN`, atomically
   (temp file + rename, mode 0600), keeping their previous contents in memory.
   Doing this first means a filesystem failure aborts with nothing changed.
3. **Re-encrypt** every secret in one DB transaction: decrypt with the old
   token, encrypt with the new. A row that doesn't decrypt aborts the whole
   rotation — and restores the env files — rather than leaving half the
   secrets keyed to a token nobody has.
4. **Swap the live token** in `settings`. From here the new token is the one
   the server checks.
5. **Drop the processes that carry the old one in their environment**: held CLI
   processes (their MCP children call back with `OCTOPUS_AUTH_TOKEN`) and
   application backends (their `OCTOPUS_APP_TOKEN` is derived from the master
   token). Both respawn on demand with the new value.

No restart: the server changes its own configuration and its own memory in the
same breath. Nothing to keep in sync by hand, so nothing can drift.

### Which env files

Whichever ones currently define the key — here that's both the file systemd
hands the service (`EnvironmentFile`) and the one pydantic reads from the
working directory. Updating only one would resurrect the old token on the next
restart, which is the worst outcome available: a server that can't decrypt its
own database.

## 3. Open tabs

Rotation broadcasts `auth_token_rotated` carrying the new token, and every
connected client stores it and carries on. That sounds alarming and isn't:
the only clients receiving it are ones already authenticated **with the old
token** — the owner's own tabs. It is the difference between "one click" and
"go and re-paste a token on four devices".

When the point of the rotation is that the old token leaked, that assumption
is wrong, so `revoke_other_clients: true` skips the broadcast and every other
client is dropped to the login screen.

Either way, clients that were offline during the rotation come back with a
dead token. That path is now handled too: a WebSocket closed with `4001`
clears the stored token and shows the login screen instead of reconnecting
every three seconds forever behind a "Disconnected" badge.

## 4. Testing, and the one thing not covered end-to-end

The re-encryption is the part that can destroy data, so it's pinned hardest:
every refusal (weak, blank, unchanged, characters that break a URL or a
cookie), the all-or-nothing guarantee (a row that won't decrypt aborts with
the env files restored and the live token unchanged), and the route in both
broadcast modes.

There is deliberately **no e2e test**. The e2e suite runs 40 tests against one
shared server with two workers; a test that rotates that server's token would
401 whatever else is mid-flight. The integration it would cover — rotate →
broadcast → client keeps working — is covered either side of the wire instead.

One scar worth recording: an early version of the env-file lookup fell back to
the working directory and the repo root even when `OCTOPUS_ENV_FILE` named a
file. Running the tests rewrote the developer's real `.env` with a test token,
which would have locked the next restart out of its own database. The lookup
now treats an explicit pointer as the *whole* list. When a module rewrites
secrets, a "helpful" fallback is a loaded gun.

## 5. What this defers

* **A CLI entry point** (`octopus rotate-token`). The server has to be running
  to do this safely — it owns the live setting and the processes that need
  dropping — so the CLI would be a thin HTTP client for the same route, and
  the Settings dialog already is one.
* **Rotating while the server is down.** Same reason: the operation is
  in-process by design.
* **Per-device tokens.** The real answer to "one device leaked" is a token per
  client, which is a different design (issue, list, revoke) and a bigger UI.
