# Inline steering — and the turn latency that pays for it

> **Implementation status: STAGE 1 SHIPPED; S2/S3 planned.** Three staged
> changes (§4). Every CLI behaviour and every number below is *measured*
> against our own `claude 2.1.272`, not assumed.
>
> S1 landed in `server/harness/claude_code.py` (the `--include-partial-messages`
> flag + `_stream_delta`), `server/session_manager.py` (`_flush_text_deltas`
> and the coalescing loop), `web/src/stores/sessionStore.ts` (`streamingText`),
> `web/src/hooks/useWebSocket.ts` (`assistant_delta`) and the ChatView footer.
> Tests: `tests/test_harness_claude.py`, `tests/test_session_manager.py`,
> `web/src/stores/sessionStore.test.ts`, `web/e2e/new-features.spec.ts`.

## 1. The idea

Today, a message sent to a running session goes into `_pending_queue`
(`server/session_manager.py:1629`) and sits there until the turn ends. Your only
way to change a turn already in flight is Esc, which throws the turn away.

**Steering** is the third option: the message reaches the agent *during* the
turn, and it adapts. "Not that file." "Skip the tests, just show me the diff."
The turn keeps its context and keeps going.

This needs no TUI and no pty. `claude --print` has a real-time input channel —
`--input-format stream-json` — which we have never used. Adopting it also
happens to be the largest speed win available to us (§3), which is why this doc
covers both.

## 2. What the CLI actually does (verified)

**A turn with no tools cannot be steered.** Steer written at 3.0s; the model
generated for 71.6s, finished the whole task, and only then consumed the steer
as a second turn.

**A turn with tools can.** Five sequential `Bash` calls:

```
[ 2.3s] TOOL Bash: sleep 4; echo STEP1
[ 6.0s] >>> STEER SENT <<<
[ 6.4s] tool_result: STEP1
[ 6.4s] <<replayed user>>: CHANGE OF PLAN. Stop after the current command...
[ 6.9s] text: HALTED
[ 6.9s] RESULT turns=2
```

Steps 2–5 never ran. **Delivery happens at the next tool-result boundary**, not
mid-generation: *a working agent is steerable; a monologuing agent is not.* Our
turns call tools constantly, so in practice it lands within seconds.

**The CLI echoes our own uuid back.** A frame stamped with a uuid we choose
returns verbatim under `--replay-user-messages` — exact dedup, not text
matching.

**`RESULT turns=2`** — a steered turn counts as two turns in one run. Our
cost/turn accounting assumes one.

## 3. What our turns actually cost (measured)

Two plausible culprits for "Octopus feels slower than native", both **wrong**:

| | measured |
|---|---|
| bare `claude --print`, trivial turn | 2.04s |
| + our 4 MCP servers (bg, ask, ask_agent, research) | **1.97s** — free |
| `--resume` a 225K / 32-line transcript | 1.82s |
| `--resume` a **16M / 4909-line** transcript | **0.86s** — free |

MCP startup costs nothing. `--resume` costs nothing, even on a huge transcript.
The only per-turn overhead we control is **process spawn**:

| | measured |
|---|---|
| turn 1, cold spawn | 2.39s |
| turn 2, same process, after a 75s idle gap | **0.90s** |
| turn 3, same process | **0.81s** |
| RSS held per live process | **261 MB** (254 MB after idle; stable) |

A persistent process is ~1.5s faster per turn *and* keeps the prompt cache warm
(`cache_read` 12.7k → 17.1k, so cheaper as well). It survived a 75s idle gap
with no keepalive.

And the reason it *feels* slow, which is neither of the above:

| time to first visible text | measured |
|---|---|
| today (complete blocks only) | **3.95s** |
| `--include-partial-messages` | **1.68s** |

We *discarded* `stream_event` outright, so the UI painted an assistant message
only when the whole block was done, while native streams tokens. Same total time
(3.96s vs 4.22s); less than half the wait before anything moves. S1 fixed this —
`ClaudeEventParser._stream_delta` now turns those frames into `text_delta`
events.

## 4. Three stages

Ordered by value-per-risk, each shippable alone:

**S1 — stream the output. SHIPPED.** `--include-partial-messages`, a
`text_delta` harness event, an `assistant_delta` WS frame, and a footer that
renders the partial answer through `MessageBubble` and drops it when the final
block lands.

Measured in the browser against the live server, not just in the CLI: on a
700-word answer the first text reached the screen at 10.4s and the completed
block at 18.8s — **8.6 seconds earlier than before**, and the win grows with
answer length (a 120-word reply led by only 0.6s). Deltas are coalesced into at
most one frame per 50ms: 128 frames for a ~900-token answer instead of ~900,
with no visible stepping. The completed answer renders exactly once.

**S2 — persist the process.** `stdin_mode=STREAM_JSON`, prompt over stdin, the
process outliving the turn, plus an idle reaper (§7). −1.5s per turn after the
first, bounded memory.

**S3 — steer.** The accept path (§8), the echo classifier (§9), the composer
change (§12). Nearly free once S2 exists — S2 is what makes the channel
writable mid-turn.

## 5. Prior art: vm0's "active input"

vm0 (now Okou) ships S2+S3. `crates/guest-agent/src/cli/command.rs:62` spawns
`claude --print --verbose --input-format stream-json --output-format stream-json
--dangerously-skip-permissions [--replay-user-messages] [--resume <id>]`. No
TUI. Their frame (`cli/mod.rs:134`):

```json
{"type":"user","uuid":"<ours>","parent_tool_use_id":null,
 "message":{"role":"user","content":"<plain string>"}}
```

Copied below: the self-stamped uuid, the four-way classification of echoed user
events, the bounded+idempotent accept path, an explicit lifecycle.

Not copied: `docs/active-input-delivery.md` is a distributed-systems design —
delivery reservations, receipt journals, webhook-proven quiescence — because
their guest runs in a remote Firecracker sandbox across a network that drops
messages. Ours is a subprocess in the same process as the session manager. The
*requirement* (never lose a steer, never apply it twice) survives; the machinery
collapses to a queue and a lifecycle flag.

## 6. The stdin channel

`HarnessRun` (`server/harness/run.py:259`) spawns with `stdin=PIPE` and closes it
immediately for both backends. `RuntimeProfile.close_stdin_after_start` becomes
`stdin_mode`:

| mode | meaning | backend |
|------|---------|---------|
| `CLOSE_AFTER_SPAWN` | prompt in argv; EOF immediately | codex |
| `STREAM_JSON` | prompt *and* follow-ups as JSON lines on stdin | claude-code |

An enum, not a second boolean: the two states are mutually exclusive and two
booleans can express a combination that must never exist.

`HarnessRun` gains `async def send_user_frame(text) -> str` (write + flush;
a write error is terminal for the turn). The initial prompt goes through the
same method with a **deterministic** uuid (`sha256(session_id:turn_seq)`) so its
echo is identifiable; `build_turn_argv` stops appending `-- <prompt>` under
`STREAM_JSON`. One path, not two.

## 7. Process lifecycle and the idle reaper

S2's cost is memory: **~255 MB per held process**. Seven live sessions is ~1.8 GB
of 15 GB — affordable, but an idle session costs zero today and must not start
costing 255 MB forever.

* The process is held by the session after its turn ends.
* An **idle reaper** closes stdin after `STEER_IDLE_TIMEOUT` (default 10
  minutes) without a turn; the CLI flushes and exits.
* The next turn respawns and `--resume`s — today's path, kept as the fallback,
  so a reaped, crashed or server-restarted process is a slow turn, never a
  broken one.
* A hard cap on concurrently held processes, reaping least-recently-used first,
  so N sessions can't pin N × 255 MB.

## 8. Accepting a steer

`SessionManager.start_message` (`:1600`) has two branches: busy → queue, idle →
run. A third is taken first on the busy path:

```
busy and harness.can_steer and steering window open  → steer
busy otherwise                                       → queue (today's path)
idle                                                 → run (today's path)
```

The window is open from spawn until `result` or `stop()`. A steer that misses it
**falls back to the queue** rather than erroring — the message is never lost, it
just arrives as the next turn. That decision must be atomic with the window
flag, or a steer racing the turn's end is delivered twice.

* **8 pending steers** per turn (vm0's number; it's a person typing).
* **Idempotent by uuid + text digest.** Same uuid + same text → no-op. Same uuid
  + *different* text → rejected loudly; silently delivering one of them is the
  worst outcome.
* **Rejected, never silently dropped**, when the backend can't steer.

## 9. Classifying the echo

`--replay-user-messages` makes the CLI emit `user` events for input we sent, and
our parser already renders `user` events as user blocks
(`server/harness/claude_code.py:281`) — so that flag and this classifier must
ship together or every prompt renders twice.

| event | outcome |
|-------|---------|
| uuid == our initial-prompt uuid | `INITIAL_ECHO` — drop |
| uuid matches a pending steer | `STEER_ECHO` — drop, mark delivered |
| carries `tool_result`, or has `parent_tool_use_id` | `EXTERNAL` — keep |
| prompt-shaped, unattributable | `UNKNOWN_PROMPT` — keep, and log |

uuid first; text only as a fallback when the uuid is absent. The fourth bucket
exists because uuid matching cannot be assumed total, and the conservative
choice for an unattributable prompt is to show it, not swallow it.

**Ordering.** A steer is persisted as a user message when *accepted*, so the
transcript matches what you saw when you hit enter; the echo is dropped. The
engine sees it slightly later (at the tool boundary) — invisible, and better
than waiting for an echo to render your own words.

## 10. The regression risk

`server/harness/claude_code.py:666-672` documents why stdin is closed today:

> leaving the pipe open made the CLI wait ~3s ("no stdin data received in 3s")
> on every turn AND — critically — made `--resume` of a freshly-synthesized fork
> transcript fail ~all the time with "No conversation found" (a discovery race
> the open-stdin wait widened).

The 3s wait **did not reproduce**: turn 1 under `STREAM_JSON` measured 2.39s,
in line with the 2.04s bare spawn, because we write the prompt frame
immediately and the CLI never waits for input that isn't coming.

The resume race is **not** settled by argument. `web/e2e/fork.spec.ts`,
`web/e2e/fork-copy.spec.ts` and the real-CLI resume tests run repeatedly before
S2 ships. If it returns, `STREAM_JSON` is gated to non-fork turns rather than
forced through.

## 11. Codex

Not in S3. `codex exec` has no mid-turn input; vm0 moved to `codex app-server
--listen stdio://` and still needed `fix(guest): terminate stuck codex steers
after timeout`. Our codex 0.132.0 marks `app-server` experimental. Codex keeps
`CLOSE_AFTER_SPAWN` and today's queue behaviour, and the composer says so.

S1 is separate: if the Codex event stream carries text deltas, it gets streaming
output too; if not, it renders as it does today.

## 12. Frontend

**S1** — assistant text arrives as deltas appended to the in-flight message, and
the final `assistant` block replaces the buffer (never appends to it, or a
dropped frame duplicates text).

**S3** — the composer already accepts input during a turn; today it queues. On a
steering-capable backend Enter sends a steer, rendered with a marker that
distinguishes it from a queued prompt ("sent to the running turn" vs "runs
next"). On other backends nothing changes.

## 13. Testing

* **S1** — parser unit tests over recorded `stream_event` fixtures (deltas
  accumulate; the final block replaces, not appends; a turn with no deltas still
  renders); a store test for buffer-then-replace; an e2e asserting text appears
  before the turn ends.
* **S2** — argv snapshots per `stdin_mode`; reaper tests (idle close, respawn on
  next turn, LRU cap); the §10 fork specs as the regression gate; a real-CLI test
  that a second turn reuses the process.
* **S3** — the classifier over fixtures (all four buckets, uuid-missing
  fallback, `tool_result` never misread as an echo); the accept path (window
  open/closed, queue cap, idempotency, reused-uuid rejection); a real-CLI test in
  the probe-2 shape (steer a multi-tool turn, assert the later tools never ran
  and `turns == 2`); an e2e steering a live session.

## 14. What this defers

* **Steering a tool-free turn.** No boundary to land on (§2), so it waits for the
  turn to end. Redirecting a monologue needs interrupt-and-resume — a different
  feature.
* **Codex steering.** §11 — needs `app-server`, experimental on both sides.
* **Attachments in a steer.** Frames carry text; files mid-turn raise path
  resolution and ordering questions that text does not.
* **Steering delegation children.** A child is driven by `DelegationManager`, not
  a human at a composer; "the parent steers the child" is an agent-protocol
  question, not a UI one.
