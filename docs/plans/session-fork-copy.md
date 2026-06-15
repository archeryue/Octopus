# /fork — duplicate a session onto a copied working directory

## 1. What & why

`/rewind` branches the *conversation* (goes back to a message, archives the
parent, reverts files in place). The new `/fork` is the **filesystem fork**: it
duplicates the CURRENT session — full conversation history — onto an
**independent full copy** of its working directory, leaving the original
session and its directory completely untouched. Use it to try changes on a
throwaway copy of the project without disturbing the original.

## 2. Behavior

- New session under the same agent/backend/credential, **renamed** (`/fork
  <name>`, default `"<parent name> (fork)"`).
- Working dir = a **literal full copy** of the parent's working dir at
  `~/.octopus/fork/<basename>-<forkid>/` (incl. .git/node_modules/.venv — the
  user chose the literal copy; created with `mkdir -p`).
- **History carried over**: all parent messages copied; the fork continues the
  conversation at the new path (per-backend resume via `harness.prepare_fork`
  — Claude synthesizes a transcript keyed to the copied dir, Codex replays).
- **Parent untouched**: NOT archived. The fork has `forked_from_session_id` set
  (lineage → nests under the parent in the sidebar). `fork_after_seq` stays set
  to the last copied seq — it doubles as the HISTORY_REPLAY cutoff, so nulling
  it would make the first backend turn replay zero context (Vera review). The
  UI instead suppresses the "@msg" badge / renders "full copy of the working
  dir" via a `fork_is_full_copy` flag derived from `fork_metadata.full_copy`.

## 3. Implementation

- `SessionManager.duplicate_session(parent_id, *, label)` — mirrors the
  `fork_session` saga MINUS the rewind/validate-target and the parent-archive,
  PLUS the dir copy:
  1. idle guard (same `_forking`/lock checks as fork_session — no active turn,
     queue, approval, delegation), claim `_forking`.
  2. snapshot the transcript (`load_messages` + `last_seq`) AFTER claiming the
     guard — earlier risks a fast turn copying a post-turn dir against a
     pre-turn message list (Vera review).
  3. `copytree` parent.working_dir → dest, in `asyncio.to_thread` (it can be
     large/slow); `rmtree` a partial dest on failure.
  4. `create_fork_session(fork_after_seq = last_seq, working_dir = dest, …)` —
     copies ALL messages, inserts the fork row (origin='fork', forked_from).
  5. `prepare_fork(all_msgs, dest, resume_id_hint, fork_id)` → set
     claude_session_id + fork_needs_replay. Compensation on failure: cleanup
     artifacts first, then delete row + `rmtree` dest; if cleanup itself fails,
     leave BOTH the 'initializing' row AND the dest for the startup sweep to
     retry idempotently (the sweep `rmtree`s the private copy once cleanup wins
     — `_is_fork_copy_dir` excludes a rewind's shared parent dir).
  6. keep `fork_after_seq` (replay cutoff); set `fork_metadata.full_copy = True`
     + fork_status='ready'.
  7. do NOT archive the parent; broadcast a `session_forked` event so other
     tabs add the new session.
- Route: `POST /api/sessions/{id}/duplicate {label?}` → returns the new
  SessionInfo (carrying `fork_is_full_copy = True`).
- Frontend: `/fork [name]` slash command → POST → add the returned session to
  the store + switch to it (parent stays). Distinct from `/rewind`. Other tabs
  fetch the full SessionInfo on the `session_forked` event.

## 4. Crash safety / limits

A crash mid-copytree leaves a partial dir under `~/.octopus/fork/` — harmless
junk (a future cleanup can sweep stale fork dirs whose session row is gone).
The fork-saga's existing `initializing`/`ready` + prepare_fork compensation
still applies. Literal copy is the user's explicit choice; document the size
cost in the command hint.

## 5. Tests

- duplicate_session: dest dir created + is a real copy; all messages copied;
  parent row/dir untouched (not archived); resume state set; fork_after_seq
  null; rejects when the parent has an active turn.
- route + /fork command (frontend).
- e2e: /fork creates a second session, original remains.
