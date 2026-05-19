# Hardening the bg-task → model-turn pipeline (2026-05-18)

Four interlocking bugs were hitting the `mcp__bg__run` →
`deliver_bg_result` → `claude` CLI path. All four are now fixed.
This doc is the post-mortem and the map to the code.

## 1. E2BIG on bg-task-result delivery

**Symptom.** A `bun run test:e2e` bg task produced ~140 KB of
stdout. `render_delivery_prompt` wrapped it in framing and
`start_message` queued it as a synthesized user prompt. The Claude
CLI was then spawned with that prompt as a single positional argv
element. `execve(2)` returned `E2BIG` because Linux's
`MAX_ARG_STRLEN` is 131 072 bytes per arg.

**Root cause.** The bg pipeline is structurally capable of
producing argv elements above the kernel's per-arg cap whenever a
task's stdout+stderr framing exceeds ~128 KB. `MAX_STREAM_BYTES`
in `bg_tasks.py` is 200 KB per stream, so worst case is ~400 KB
of prompt — over 3× the cap.

**Fix.** New module `server/large_prompts.py`:
`spill_if_large(session_id, prompt)` writes any prompt over
`LARGE_PROMPT_THRESHOLD_BYTES` (100 KB) to
`~/.octopus/large-prompts/<session>/<uuid>.txt` and returns a small
pointer message instructing the model to `Read` the file. Wired in
at `session_manager.send_message`; cleanup hangs off
`delete_session`. Preserves the `[bg-task-result]` marker for the
frontend's auto-badge.

Tests: `tests/test_large_prompts.py` (12 cases).

## 2. CLI premature-exit-after-tool-use

**Symptom.** Periodically (the doc that used to be
`docs/cli-resume-synthetic-pair.md` chronicled this in depth) the
`claude --print` invocation would emit a `tool_use` event, run the
tool, and then exit *without* re-invoking the model or emitting a
`result` event. The chat just went silent.

**Fix.** `session_manager._run_backend` is now a loop. We track
`saw_result` and `saw_tool_use` across the event stream. If the
stream ends without a `result` after a `tool_use` AND we have a
resume id, we respawn the CLI once with prompt `"continue"` and
keep yielding events. Bounded by `_MAX_RECOVERY_ATTEMPTS = 1`. The
recovery turn surfaces as a small system marker `(auto-resumed
after CLI exited mid-turn)` so the user sees what happened.

Also added: a `session_started` BackendEvent emitted on the CLI's
`init` event so the resume id is captured *before* `result`
(which can be the missing event).

Tests: `test_session_manager.py` —
`test_run_backend_auto_respawns_on_premature_exit_after_tool` plus
three guard tests.

## 3. Bg orchestration camping on chip-`running` after the shell goes idle

**Symptom.** A `pytest tests/ -q` bg task would print
"408 passed in 63.77s" and then sit in the `running` state for
6+ minutes. The shell wrapping pytest didn't actually exit — pytest
seemingly hung in atexit (likely an aiosqlite worker thread not
joining). `proc.wait()` blocked. Only a manual cancel got the chip
to finalize.

**Fix.** Two things in `server/bg_tasks.py`:

- **Idle watchdog.** A background task tracks the wall-clock time
  of the last byte read on either stdio pipe. If
  `IDLE_AFTER_OUTPUT_TIMEOUT_SECS` (60s) of silence elapses *after*
  the command has produced at least one byte AND the proc is
  still alive, we SIGTERM the pgrp. Commands that never produce
  output (e.g. `sleep 300`) are exempt — the clock only starts on
  first byte.
- **`interrupted` status.** Distinguishes "killed by external
  signal we didn't initiate" from "the command itself exited
  non-zero". The status string already existed for orphaned-on-
  startup tasks; we now also use it for signal-killed-with-no-
  internal-flag. The chip label is now honest about what happened.

Mirrors the pattern from VM0's `cli/termination.rs` (we keep a
forced-termination grace machine; they arm theirs on protocol
events like `type=result`, we arm ours on stdio-quiet because bg
tasks don't have a protocol signal).

Tests: `test_bg_tasks.py` —
`test_idle_watchdog_terminates_proc_that_goes_silent`,
`test_idle_watchdog_does_not_fire_on_quiet_short_command`, and
`test_external_sigterm_yields_interrupted_status`.

## 4. asyncio `StreamReader` 64 KB per-line cap

**Symptom.** During an unrelated turn, the Claude CLI emitted a
single stream-json event larger than 64 KB. asyncio raised
`LimitOverrunError: Separator is not found, and chunk exceed the
limit`, the stdout reader crashed, and the turn died abnormally.
Auto-respawn (fix #2) caught it, but every recurrence wasted a
CLI invocation.

**Root cause.** `asyncio.create_subprocess_exec` defaults
`StreamReader._limit` to `2 ** 16` bytes per line. Anything larger
between newlines raises. Tool-result blocks carrying big `Read`
outputs routinely exceed this.

**Fix.** `server/backends/subprocess_jsonl.py`: pass
`limit=_STDOUT_LINE_LIMIT_BYTES` (4 MiB) to
`create_subprocess_exec`. Backpressure on the pipe still bounds
memory; we just stop crashing on legitimately big lines.

## What's still on the to-do list

- The pytest atexit hang itself is a real bug in pytest/aiosqlite
  test cleanup. The idle watchdog contains the *symptom*; finding
  what thread is keeping the process alive would let us close the
  underlying issue. Filed mentally as a follow-up; not blocking.
- `docs/cli-resume-synthetic-pair.md` is now superseded by fix #2
  for everything except historical context. Read with Grep, not
  full Read (it's 42 KB and still triggers the very bug it
  documents on naïve full-reads).
