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

## What's still open (deliberately out of scope)

- The pytest atexit hang itself — root cause is the aiosqlite
  worker thread spawned by `test_tunnel.py::TestCloudflaredStartSuccess::test_timeout_when_no_url_found`,
  which crashes after the event loop closes (the warning trace
  finally fingered it in tonight's pytest run). Containing the
  *symptom* via the idle watchdog was the right call for tonight;
  fixing the underlying test cleanup is a follow-up.
- `docs/cli-resume-synthetic-pair.md` (the 42 KB doc that
  reliably crashed me on full-read earlier this session) is now
  *operationally* superseded by the auto-respawn loop, but the
  doc itself is still on disk for historical reference. Read with
  Grep, not full Read.
