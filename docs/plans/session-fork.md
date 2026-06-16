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
- **History carried over (native-copy)**: the fork **resumes the parent's real
  conversation**, not a replay. `harness.prepare_fork_copy` copies the backend's
  NATIVE transcript into a fresh resume id at the fork's location, so the fork's
  first turn continues with genuine context:
  - **Claude**: copy `~/.claude/projects/<parent-slug>/<id>.jsonl` →
    `<dest-slug>/<new-id>.jsonl`, rewriting each line's `cwd`→dest and
    `sessionId`→new id. (Empirically the CLI resumes a copied *real* transcript
    fine — the old "No conversation found" was specific to *synthesized* ones.)
  - **Codex**: copy the rollout (`CODEX_HOME/sessions/.../rollout-*-<id>.jsonl`)
    → a new rollout with `session_meta.id`→new id. Codex resume is by id
    (cwd-independent), so the working-dir copy is irrelevant to it.
  - **Fallback**: if the parent has no native transcript yet (never ran a turn),
    `prepare_fork_copy` returns `needs_replay=True` and we use the old replay
    path — history is tiny/empty then, so no transcript-dump problem.
  This replaced the original whole-history *replay*, which dumped the entire
  transcript into the first prompt — for a large session that spilled to a file
  the model then mis-read as a task list and went off doing the wrong thing.
- The **Octopus `messages` table** (UI history) is copied independently
  (`create_fork_session`) and is unrelated to the native transcript: the DB
  feeds the sidebar/chat, the transcript feeds the model. Both are snapshotted
  at fork time and evolve per-session afterward — the same two stores every
  session already keeps, just duplicated.
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
  5. resolve the parent credential (Codex needs it for CODEX_HOME), then
     `prepare_fork_copy(parent_working_dir, parent_resume_id, parent_credential,
     dest, new_resume_id)` → native-copy the transcript; set claude_session_id
     (= new id on success, None on replay-fallback) + fork_needs_replay.
     Compensation on failure: cleanup artifacts first (passing the credential so
     Codex can find its rollout store), then delete row + `rmtree` dest; if
     cleanup itself fails, leave BOTH the 'initializing' row AND the dest for the
     startup sweep to retry idempotently (the sweep `rmtree`s the private copy
     once cleanup wins — `_is_fork_copy_dir` excludes a rewind's shared parent
     dir; the sweep resolves the credential from the row for Codex cleanup).
  6. keep `fork_after_seq` (= replay cutoff for the fallback path + the DB
     message-copy bound); set `fork_metadata.full_copy = True` + status='ready'.
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
  parent row/dir untouched (not archived); resume state set; `fork_after_seq`
  KEPT (= last copied seq, the replay cutoff) with `fork_metadata.full_copy`
  driving the UI; native-copy success path (parent has a transcript →
  needs_replay False + new resume id) and replay fallback (no transcript);
  rejects when the parent has an active turn; compensation (copy/cleanup
  failure) leaves no orphans / leaves row+dir for the sweep.
- harness `prepare_fork_copy` / `_fork_copy` / `_fork_cleanup` per backend:
  transcript/rollout copy + id/cwd rewrite + fallback + cleanup (unit); the
  Claude slug pinned to the real-CLI mapping.
- gated real-CLI: copy → resume → recall a codeword (both backends; skips on a
  missing CLI or an environmental auth/rate-limit failure).
- route + /fork command (frontend); e2e: /fork creates a second session,
  original remains; deferred /fork while busy fires on idle.
