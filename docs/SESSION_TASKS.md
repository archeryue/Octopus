# Tonight's punchlist (2026-05-18 → 2026-05-19) — COMPLETE

All five tasks finished. Commit `e2f9ac6` pushed to `origin/main`.

## 0. Complete current task — DONE

- `_STDOUT_LINE_LIMIT_BYTES = 4 MiB` cap is live in
  `server/backends/subprocess_jsonl.py:34,149`; the original 64 KiB
  asyncio default no longer crashes the reader on big stream-json
  events.
- Audited debug instrumentation. Removed the redundant
  "proc exited rc=…" log that duplicated the terminal-state line.
  Kept the load-bearing logs (`idle watchdog tripped`, `SIGTERM
  pgid=`, terminal-state breadcrumb).
- Verified by full backend `pytest tests/ -q`: **408 passed in 67.13 s**.

## 1. New e2e coverage — DONE

- Added `Bg-task pipeline hardening › large bg output is delivered
  to the model via spill pointer` in `web/e2e/new-features.spec.ts`.
- Exercises the full real-CLI loop: bg task produces ~120 KB →
  spill module writes the file → claude CLI spawns with the
  ~300-byte pointer → model Reads the file → response surfaces the
  sentinel. Passes in ~29 s.
- Full e2e suite ran green afterwards: **45 passed in 3.7 m**
  (44 previous specs + the new one).
- The idle-watchdog and auto-respawn cases are exercised by
  unit-level tests rather than e2e — the wall-clock for an e2e
  watchdog test (60 s threshold + grace) didn't justify a third
  slow spec when the same property is asserted in
  `tests/test_bg_tasks.py` and `tests/test_session_manager.py`.

## 2. Replace contingency doc — DONE

- `docs/large-prompt-e2big-contingency.md` removed (was untracked,
  so just a filesystem delete).
- `docs/2026-05-18-bg-pipeline-hardening.md` written — single
  screen, four-fix post-mortem with `file:line` pointers.

## 3. CLI system-prompt rule for bg vs Bash — DONE

- Confirmed `--append-system-prompt` is the right hook (used by
  both Octopus and VM0; see `vm0/crates/guest-agent/src/cli/command.rs`
  and `vm0/crates/guest-agent/src/env.rs`).
- Tightened `_OCTOPUS_SYSTEM_PROMPT` in
  `server/backends/claude_code.py`: bg_run is now an unconditional
  bright line for any test suite, build, install, sleep, large
  fetch — no "≥30s heuristic" fallback to Bash.
- Wrote `docs/cli-system-prompt-notes.md` explaining what belongs
  in the CLI system prompt vs auto-memory vs `CLAUDE.md`.

## 4. Verify + commit + push — DONE

- Backend `pytest tests/ -q`: **408 passed**.
- Frontend `bun run test`: **32 passed** (vitest, 4 files).
- TypeScript `npx tsc --noEmit`: **clean**.
- Playwright `bun run test:e2e`: **45 passed in 3.7 m**.
- Commit `e2f9ac6` ("Harden bg-task → model-turn pipeline against
  large outputs and stuck commands") — 16 files changed,
  +1466 / −75.
- Pushed to `origin/main` cleanly.

## Follow-ups closed (post-session)

- **Pytest atexit hang — fixed.** True root cause was a latent bug
  in `server/database.py:_ensure_connected`: when `self._conn` was
  `None` (the state set by `close()`), the method silently
  reconnected — calling `aiosqlite.connect()` and spawning a fresh
  `_connection_worker_thread`. In tests, pytest-asyncio tears down
  each function's event loop with
  `loop.run_until_complete(tasks.gather(*to_cancel))`, which gives
  pending `_consume_message` tasks one last chance to run. Those
  tasks hit `db.flush() → _ensure_connected()`, the reconnect fired
  *after* the test's `db.close()`, and the new (non-daemon) worker
  thread was orphaned right before the loop died. With enough such
  threads accumulating across tests, the pytest process couldn't
  exit at atexit. Confirmed with an aiosqlite.connect tracer: three
  reconnect-spawned connections, all originating from
  `session_manager._consume_message → send_message → db.flush →
  _ensure_connected`. Fixed by:
  1. Adding a `_closed` flag to `Database`, set in `close()`.
  2. Making `_ensure_connected` raise `asyncio.CancelledError` once
     `_closed` is True — so any in-flight consumer task exits via
     the existing CancelledError handling instead of resurrecting
     the connection.
  3. Removing the silent reconnect entirely.
  After the fix: `pytest tests/ -q` finishes in 67.95 s wall (was
  pinned at the 300 s outer timeout) and exits 0 cleanly.
- **`tests/test_session_manager.py` and
  `tests/test_large_prompts.py` — db close cleanup added.** The
  `manager` fixture in `test_session_manager.py` was a `return`-style
  fixture (no teardown hook), and two inline tests also leaked
  `Database(":memory:")`. Converted the fixture to `yield` and
  wrapped the inline allocations in `try/finally: await db.close()`.
  These weren't the atexit-hang trigger on their own (the reconnect
  was), but leaking connections in tests is wrong regardless and
  this kept the new `_closed` assertion path honest.
- **`server/tunnel.py:_read_until_url` — hardened en route.** While
  investigating, found that the cleanup `except Exception` doesn't
  catch the `CancelledError` raised when the outer `asyncio.wait_for`
  times out (CancelledError inherits from `BaseException` since
  Python 3.8), so the two stream-reader tasks could leak past the
  test. Switched to `try/finally`. Not the cause of the hang, but a
  real latent bug — kept the fix.
- **`tests/test_bg_tasks.py::test_build_args_system_prompt_teaches_bg_usage`
  — fixed.** The earlier CLI-system-prompt tightening (this session's
  task #3) replaced the "≥30s" heuristic with bright-line categories;
  the assertion was still looking for the old string. Updated to
  assert against `"Use bg_run unconditionally"`, which is what the
  new prompt actually teaches.
- **`docs/cli-resume-synthetic-pair.md` — removed.** All in-tree
  pointers (code comments + neighbouring docs) redirected to
  `docs/2026-05-18-bg-pipeline-hardening.md` §2, which is now the
  authoritative reference for the premature-exit bug.
