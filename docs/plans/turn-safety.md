# Turn safety (Layer 1): timeout + process-group reaping

## 1. Why

A session ("stock") ran a long, tool-heavy operation (the `/deep-research`
skill) and the turn **hung forever with no output until the user interrupted**.
Root cause, two Octopus gaps (independent of deep research):

1. **No per-turn timeout.** The stream consumer blocks on
   `await self._event_queue.get()` (`run.py`) and `_run_backend`'s
   `async for event in backend.stream()` (`session_manager.py`) with no
   timeout. A turn that emits no events (a wedged tool, a background workflow
   that never reports a terminal result) blocks indefinitely. The only existing
   net is the 30-min `AskUserQuestion` auto-answer, which never fires here.
2. **No process-group reaping.** The turn subprocess is spawned without
   `start_new_session=True` and `stop()` only `terminate()/kill()`s the direct
   child (`run.py:225,270`). Nested children the CLI spawns (subagents, MCP
   servers) are **orphaned** on interrupt rather than reaped.

This is also the hard prerequisite for native-deep-research.md, whose web
"leaves" are real harness sub-turns that must be individually bounded and
reaped.

## 2. Process-group reaping (both turns and `run_oneshot`)

- **Own process group:** add `start_new_session=True` in `prepare_spawn`
  (`run.py`) — the single spawn seam shared by the streaming engine
  (`run.py:222`) AND `Harness.run_oneshot` (`harness.py:152`). The child
  becomes a session/group leader, so its descendants share its pgid.
- **Reap the group:** a `_terminate_process_group(proc)` helper does
  `os.killpg(os.getpgid(pid), SIGTERM)` → wait → `SIGKILL` the group, guarded
  for `ProcessLookupError`/`PermissionError` with a fallback to the old
  per-proc `terminate()/kill()`. `HarnessRun.stop()` uses it (keeping the
  stdin-close-first graceful step); `run_oneshot`'s timeout path kills the
  group too. bg_tasks already uses process groups (`bg_tasks.py`) — same idea.

## 3. Per-turn watchdog (idle + overall)

In `_run_backend`, around the `async for event in backend.stream()`:

- Track `last_event_at` (reset on every event) and the turn `start`.
- A watchdog task (also the only timer we need) wakes periodically and, if
  `now - last_event_at > turn_idle_timeout_seconds` OR
  `now - start > turn_max_seconds`, records the reason and calls
  `backend.stop()` — which emits `_STREAM_END`, so the `async for` exits
  cleanly (no generator-cancellation hazard).
- After the loop, if the watchdog tripped: surface a clear, honest error
  (`code:"turn_timeout"`, idle vs overall in the text), persist it, and
  `return` — **before** the auth/transient/premature-exit dispatch, so a timed-
  out turn is never mis-read as transient and never respawned with "continue".

Config (settings, 0 = disabled): `turn_idle_timeout_seconds` (default 300 —
generous: a single web sub-turn can legitimately take tens of seconds) and
`turn_max_seconds` (default 1800). These bound *any* turn, not just research.

## 4. Interaction with existing paths

- Auth-expiry / transient-retry classification and premature-exit recovery are
  unchanged; the timeout check sits ahead of them and returns, so they don't
  fire on a timeout.
- Interrupt is unaffected except that it now reaps the whole group (strictly
  better).
- `start_new_session=True` matches what `codex_login` and bg-tasks already do;
  no behavior change for healthy turns beyond detaching into a pgroup.

## 5. Testing

- Reaping: a fake/scripted child that spawns a grandchild; assert the group is
  signalled (monkeypatch `os.killpg` to record, or spawn a real `sh -c 'sleep'`
  tree and assert it dies). `prepare_spawn` now sets `start_new_session=True`.
- Watchdog: a fake backend whose `stream()` stalls (never yields) → assert the
  turn ends within the idle timeout with a `turn_timeout` error event and that
  `backend.stop()` was called; a backend that yields slowly but within idle is
  NOT killed; overall-cap trip with steady events.
- Ordering: a timed-out turn does not enter premature-exit recovery.
- Full backend + frontend suites.

## 6. What this defers

- **Heartbeat / "still working (Nm)" UI** — a separate UX nicety (needs a
  frontend rendering decision). The timeout already removes the indefinite
  hang; a progress heartbeat is additive and tracked separately so this unit
  stays focused on correctness. (native-deep-research surfaces its own
  per-phase progress regardless.)
- Per-tool / per-MCP timeouts (finer-grained than the turn) — not needed for
  the hang; revisit only if a specific tool class warrants it.
