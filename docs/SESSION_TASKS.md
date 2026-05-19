# Tonight's punchlist (2026-05-18 → 2026-05-19)

Five tasks, in order. Each one verified before moving on.

## 0. Complete current task

End the live verification cycle cleanly:

- Confirm the just-shipped `StreamReader` cap (`64 KB → 4 MiB` in
  `server/backends/subprocess_jsonl.py`) is live in the running
  server now that it has been restarted, and that no further
  `LimitOverrunError` crashes occur on bg-task-result delivery.
- Make sure the working tree has no stale debug instrumentation left
  over from the investigation (extra `logger.info` lines that won't
  carry their weight in production).

## 1. New e2e coverage for tonight's fixes

Write Playwright e2e tests that exercise each shipping fix end-to-end
through the real UI + backend, and run them yourself:

- Bg task with a huge captured output → the chip stays green, no
  E2BIG; the model's follow-up turn arrives as a `[octopus-large-prompt]`
  pointer and the spilled file is readable.
- Bg task whose command produces output and then sleeps past the
  idle-watchdog threshold → chip lands on `interrupted`, not `failed`.
- Auto-respawn on CLI premature-exit-after-tool-use → user sees the
  `(auto-resumed after CLI exited mid-turn)` marker and the turn
  continues coherently.

Definition of done: `bun run test:e2e` passes locally, including the
new specs, with no regressions in the existing 44 cases.

## 2. Replace the long contingency doc with a concise tonight-recap

- Delete `docs/large-prompt-e2big-contingency.md` (it's superseded —
  the contingency we wrote there *did* get triggered and we acted).
- Write a single concise document (one screenful, no more) covering:
  - The issues we hit tonight (E2BIG on bg deliver; CLI
    premature-exit-after-tool-use; pytest atexit hang stalling bg
    orchestration; asyncio `StreamReader` 64 KB cap).
  - For each, the one-line root cause and the one-paragraph fix.
  - Pointers to the code (file:line) and to the relevant tests.
- Suggested filename: `docs/2026-05-18-bg-pipeline-hardening.md`.

## 3. CLI-level system-prompt rule for bg vs Bash

The "always use `mcp__bg__run`, never Bash for long commands" rule
currently lives only in user-scoped auto-memory — which means a fresh
Octopus session by a different user/clone is back to step zero.
Move the rule to where every CLI invocation reads it:

- Study how Octopus already injects `_OCTOPUS_SYSTEM_PROMPT` into the
  `claude` CLI via `--append-system-prompt` (see
  `server/backends/claude_code.py`). Confirm this is the right hook
  (not modifying user memory, not modifying CLAUDE.md).
- Cross-check what VM0 does — they have the same need and we copied
  this command shape from them (`vm0/crates/guest-agent/src/cli/...`).
  Specifically check whether they have any equivalent rule at the
  CLI system-prompt layer.
- Add the bg-vs-Bash rule into the appended system prompt so every
  `claude` invocation we spawn sees it on every turn.
- Write a short doc (`docs/cli-system-prompt-notes.md` or similar)
  explaining what we append at the CLI level vs what we keep in
  user/project memory, and why.

## 4. Verify + commit + push

- Re-read each of tasks 0-3 and confirm done.
- Run the full backend `pytest tests/ -q` and frontend
  `cd web && bun run test` + `bun run test:e2e` — all green.
- Stage and commit the changes (one commit per logical piece is
  preferred; bundle if the pieces don't make sense alone).
- Push to `main`.

After push: nothing else queued.
