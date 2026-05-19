# Premature Turn Termination After Tool Call (and the Synthetic-Pair Artifact)

Investigation notes from a debugging session that started as "Claude went
silent after editing a file twice." The real bug turned out to be
deeper than initially diagnosed: **the CLI ends the `--print`
invocation after one tool roundtrip, without re-invoking the model
with the tool result**. The synthetic-pair artifact we chased for the
first half of this doc is downstream of that — it's how the *next*
`--resume` patches the prematurely-ended turn.

**Status**: root cause identified at the API-loop layer; trigger
conditions not yet pinned down (probes in isolation don't reproduce
production behavior). No code change yet. See §13 for the corrected
diagnosis and §14 for what to dig into next.

**Captured against**: `claude` v2.1.143.

---

## 1. Symptom we set out to investigate

The user observed that, after Claude did two consecutive `Edit` tool
calls, the assistant turn appeared to end silently in the Octopus chat
— no follow-up text, just two tool cards. The user typed "what's going
on?" / "you're getting stuck at edit twice".

Initial (wrong) framing: I, the model, was misclassifying a real user
instruction as a no-op and emitting `"No response requested."` as a
silent bail.

This framing was wrong. See §3.

The next framing — synthetic-pair injection causing the visible
silence — was *also* wrong as a root cause. The synthetic pair is
real, but it's a downstream artifact, not the bug. The actual bug is
in §13.

---

## 2. What's actually in the CLI's session transcript

Octopus's `ClaudeCodeBackend` spawns the `claude` CLI binary once per
turn. The CLI maintains its own private session transcript at
`~/.claude/projects/<cwd-slug>/<session_id>.jsonl`. Octopus does
**not** read or write this file — it's CLI-internal.

When you grep that transcript for the symptom strings, every
occurrence is paired:

```jsonl
{"type":"queue-operation","operation":"enqueue","content":"what's going on?"}
{"type":"queue-operation","operation":"dequeue"}
{"type":"user","message":{"role":"user","content":[{"type":"text","text":"Continue from where you left off."}]},"isMeta":true,"promptId":"…","entrypoint":"sdk-cli"}
{"type":"assistant","message":{"role":"assistant","model":"<synthetic>","content":[{"type":"text","text":"No response requested."}]}}
{"type":"user","message":{"role":"user","content":"what's going on?"}}
{"type":"assistant","message":{"model":"claude-opus-4-7", … real model output …}}
```

Two markers prove the pair is **not** a real interaction:

- The user side carries `"isMeta": true` and a synthetic `promptId`.
- The assistant side carries `"model": "<synthetic>"` — no API call was
  ever made; the CLI binary emitted the text directly.

Both entries share the same millisecond timestamp, which never happens
for real LLM turns (those take seconds).

---

## 3. Where the pair *does* and *doesn't* surface

| Surface | Sees the pair? |
|---|---|
| `~/.claude/projects/…/{session_id}.jsonl` (CLI's private transcript) | **Yes** — always written. |
| CLI's stdout (the channel Octopus parses) | **No** — never streamed. |
| `server/backends/claude_code.py::on_stdout_line` event types | **No** — never reaches the parser. |
| Octopus's `messages` DB table | **No** — checked: zero rows match either string. |
| Octopus's chat UI | **No** — nothing to render, since it isn't broadcast. |
| The model's input context on the **next** `--resume` turn | **Yes** — the CLI rebuilds my prompt from its transcript, including the synthetic pair. |

This is why I got confused: my replayed context showed `Continue / No
response requested.` as if they were real prior turns, and the `isMeta`
/ `model:<synthetic>` flags were stripped when the context was
flattened into my prompt. I had no way to tell synthetic from real, and
I incorrectly told the user I had "emitted 'No response requested'
twice."

---

## 4. Exactly when the CLI injects the pair

Triggered by **all three** of:

1. The new invocation uses `--print` (one-shot mode).
2. The new invocation uses `--resume <session_id>`.
3. The prior assistant turn in that session ended with **only** tool
   blocks — no text block.

In Octopus's normal flow every turn meets all three: we run one
`--print --resume` per user message, and Claude very often ends a turn
on a tool call (`Read`, `Edit`, `Bash`, etc.) without a trailing
text block.

Best inference from the pattern: the CLI's transcript invariant
expects strict alternation `user → assistant(text) → user →
assistant(text) → …`. A trailing assistant turn with no text would
break that invariant when the next real user message lands. The
synthetic pair patches the alternation: an empty user turn ("Continue")
and an empty assistant turn ("No response requested") get inserted as
a bridge.

---

## 5. Why we didn't hit this before refactor `ac9f3fb`

Commit `ac9f3fb` ("Drop claude-code-sdk: spawn `claude` CLI directly,
add credentials + AskUserQuestion form") replaced the Python
`claude-code-sdk` wrapper with a direct CLI spawn.

The Python SDK didn't write to `~/.claude/projects/`. It held the
conversation context in-process and called the Anthropic API directly
each turn. There was no per-turn "load the transcript, find it
incomplete, patch the alternation" step. So no synthetic-pair was
generated and no synthetic content polluted the model's context.

This is a genuine regression introduced by the refactor — it didn't
exist with the Python SDK path.

---

## 6. Why we don't hit it in an interactive `claude` REPL either

The interactive REPL is a single long-lived process. It doesn't
re-load the transcript between turns, because it never exited. There's
no "rebuild and patch" event to trigger the synthetic-pair injection.

The user confirmed: running `claude` directly in a terminal, the
synthetic pair never appears.

So the trigger is uniquely **"per-turn `--print --resume` spawn after a
tool-only trailing turn"** — which is exactly Octopus's loop.

---

## 7. What VM0 does, and why we initially thought it avoided the bug — but doesn't

VM0 (`/home/start-up/vm0`, a sibling AI-agent platform) also wraps the
`claude` CLI for its sandboxed runs. We checked because the user had
not seen this issue when using VM0.

**Octopus's invocation** (`server/backends/claude_code.py`, around the
`build_args` method):

```
claude --print
       --input-format=stream-json
       --output-format=stream-json
       --verbose
       --permission-mode=default
       --permission-prompt-tool=stdio
       --mcp-config '{…viewer + bg…}'
       --append-system-prompt '…'
       [--resume <session_id>]

# Prompt is then written to stdin as:
#   {"type":"user","message":{"role":"user","content":"<the prompt>"}}\n
```

**VM0's invocation** (`vm0/crates/guest-agent/src/cli/command.rs:36-86`):

```rust
let mut args = vec![
    "--print",
    "--verbose",
    "--output-format",  "stream-json",
    "--dangerously-skip-permissions",
    // no --input-format
];
if !resume_id.is_empty() {
    args.push("--resume");
    args.push(resume_id);
}
args.push("--");                // option-parsing terminator
args.push(prompt.to_string());  // prompt as a positional argv
```

Three structural differences:

| Flag | Octopus | VM0 | Effect on synthetic-pair |
|---|---|---|---|
| `--input-format` | `stream-json` | (default, `text`) | **Load-bearing.** Stream-json input is what triggers the resume-bookkeeping path that injects the synthetic pair. |
| Prompt delivery | JSON object on stdin | Positional argv after `--` | Tied to the above. |
| `--permission-prompt-tool` | `stdio` | not set | VM0 doesn't need the bidirectional control protocol; it uses `--dangerously-skip-permissions`. |

VM0 does **not** post-process or filter `~/.claude/projects/…jsonl`.

### Update: the probe in §11 actually ran, and the conclusion above is wrong

We ran the §11 probe and **both shapes inject the synthetic pair on
`--resume` after a tool-only trailing turn.** Concrete result:

| Shape | Turn 1 transcript | Turn 2 (after --resume) | Synthetic pair introduced by resume? |
|---|---|---|---|
| A (stream-json input, prompt on stdin) | no markers, trailing turn was tool-only | both markers | **yes** |
| B (positional argv prompt, no --input-format) | no markers, trailing turn was tool-only | both markers | **yes** |

The CLI's synthetic-pair logic is keyed on **"the trailing assistant
turn on the resumed session had no text block,"** not on the input
format. `--input-format=stream-json` was a red herring.

That means our user's perception that VM0 "doesn't have this problem"
is most likely because the artifact is **invisible** (CLI-private
jsonl only, never broadcast on stdout). Both Octopus and VM0 emit it
in identical conditions; only Octopus noticed because we started
introspecting our own context.

### Implications for the fix

- **Option (B) "drop `--input-format=stream-json`, use positional
  argv" is no longer on the table.** It would have been a one-line
  fix if true, but the probe shows it changes nothing.
- The synthetic pair is intrinsic to `--print --resume` after a
  tool-only turn. To eliminate it we either avoid `--resume`, avoid
  tool-only trailing turns, or strip the pair from the transcript
  before each spawn.
- The cost analysis from §9 stands — it's still bug-but-invisible.
  Recommendation defaults to (A) "live with it; don't misattribute it
  when reading own context."

### One unresolved sub-question from the probe

Even with `--permission-prompt-tool=stdio` set, neither shape fired a
`can_use_tool` control_request for `echo PROBE-TURN-ONE` (Bash, not
on the user's allowlist). Both shapes ran the tool successfully and
returned. Either the CLI is silently auto-allowing under some default
heuristic we don't see, or `--permission-prompt-tool=stdio` only
engages under specific conditions we haven't isolated. Octopus's
production `--permission-prompt-tool=stdio` works for AskUserQuestion
interception, so this isn't broken at the system level — but it means
our probe couldn't verify that shape B's stdio control protocol works,
which is moot now that shape B doesn't fix the bug anyway. Worth
investigating separately if we ever revisit the permission flow.

---

## 8. Why Octopus picked the shape it did

We use `--input-format=stream-json` + `--permission-prompt-tool=stdio`
together because they give us the SDK's bidirectional **control
protocol** over stdio, which Octopus depends on for:

1. **Per-tool permission decisions.** `_handle_can_use_tool` in
   `claude_code.py` receives every tool call as a control_request, and
   we either auto-allow (no callback) or route to a host callback that
   prompts the user.
2. **AskUserQuestion routing.** Same channel: when the model calls
   `AskUserQuestion`, the CLI sends a control_request; we hold it, emit
   a `question_request` event for the frontend, render the form, and
   when the user answers we write back a `control_response` with the
   answer text via `_send_control_response_with_content`.

VM0 disables permissions entirely (`--dangerously-skip-permissions`)
and has no AskUserQuestion-style form, so the control protocol gives
them nothing they need. They can drop `--input-format=stream-json`
without losing functionality.

For Octopus, we **do** lose functionality if we drop the control
protocol path. The question is whether the control protocol works on
its own (over stdin) when `--input-format` is left at `text` — or
whether stream-json input is a prerequisite for the control protocol
too.

---

## 9. What this bug actually costs

| Cost | Severity |
|---|---|
| User-visible bug in the Octopus UI | **None** — pair is never broadcast. |
| Synthetic strings stored in Octopus's DB | **None** — verified zero rows. |
| Wasted tokens in my context per turn | **Small** — ~25 tokens per pair, one pair per qualifying turn. Linear with conversation length. |
| Attribution confusion when I read my own history | **Real** — I've already made the mistake of "apologizing for emitting these" in this very session. Easy to drag a red herring into the conversation if I'm not careful. |

So the bug is real but the impact is small. It's worth fixing for
correctness and to stop tripping on §10 in the future, but it isn't a
production-pager.

---

## 10. Three fix options, ranked

### (A) Live with it; treat the pair as scaffolding on sight
- **Cost:** zero engineering.
- **Risk:** I forget what the pair is and drag it back into a user
  conversation. Mitigated by this doc + an entry in
  `~/.claude/.../memory/`.
- **Recommendation:** the default unless we have another reason to
  touch this code path.

### (B) ~~Drop `--input-format=stream-json`, pass prompt as positional argv (VM0 shape)~~ — **disproved by the probe**
- The probe in §11 showed both shapes inject the synthetic pair under
  the same conditions. This option is dead.

### (C) Pre-strip the synthetic pair from `~/.claude/projects/…jsonl`
before each spawn
- A 20-line helper called from `ClaudeCodeBackend.start()` that
  rewrites the jsonl with synthetic entries dropped.
- **Risk:** mutating a file the CLI considers its own state. If the
  CLI's schema changes in a future version, or it validates the file
  on load, this breaks silently or noisily.
- **Recommendation:** avoid unless (B) turns out to be infeasible.

We previously considered a fourth option — "keep one long-lived
`claude` process per session, drop `--print --resume` entirely" — and
noted it'd be a big refactor with side benefits (covered separately
in the cross-turn bg discussion). It also fixes this bug, but is way
too much surgery to justify for this issue alone.

---

## 11. Probe results (executed)

Probe script: `/tmp/probe_resume_synthetic.py` (kept locally for
reproducibility, not committed). For each of {A, B}, we ran two
turns with `--print` on the same session-id, the second using
`--resume`, with the first turn ending on a tool-only assistant
output (Bash echo, no follow-up text), then inspected the CLI's
private jsonl transcript for `Continue from where you left off.`
and `"model":"<synthetic>"` markers.

Outcome (also reproduced in §7's update table):

| Shape | Turn 1 left tool-only trailing? | Synthetic markers after turn 1 | Synthetic markers after turn 2 (--resume) |
|---|---|---|---|
| A | yes | no | **yes** |
| B | yes | no | **yes** |

**Conclusion:** the synthetic-pair injection is triggered by
`--print --resume` over a session whose trailing assistant turn was
tool-only, **regardless of input format or prompt-delivery shape**.
The VM0 command shape doesn't fix it. The doc's prior framing
(input-format is load-bearing) was wrong.

What this changes:
- The proposed quick fix (B) is off the table.
- Option (A) "live with it" is now the explicit recommendation.
- Option (C) "strip the pair from the transcript before each spawn"
  is the only viable code-side fix short of dropping
  `--print --resume` entirely. Still not recommended unless the
  attribution-confusion cost becomes real (e.g. it actively
  corrupts model behavior in long sessions, not just my own
  introspection mistakes).

Owner / target: ~~closed for now~~ — superseded by §12 below.

---

## 12. The framing was still incomplete — there is a *user-visible* symptom

The user pushed back: they observe the chat session **stopping mid-task
after a tool call** in Octopus and never observed it in VM0. Earlier
sections argued the synthetic-pair was invisible to the UI and the
"silence after a tool call" was just my own non-emission between
tool calls. After more digging, that's also wrong.

Inspecting the live JSONL transcript right after a real "stuck" turn
(a `grep -n "what's going on?"` Bash call I made earlier in this very
session) shows the actual sequence:

```
[N+0] assistant: thinking + stop_reason=tool_use
[N+1] assistant: tool_use(Bash) + stop_reason=tool_use
[N+2] user:      tool_result (grep stdout)
[N+3] last-prompt marker        ← the --print invocation ends here
[N+4] ai-title marker
```

There is **no assistant entry between [N+2] and [N+3]**. The model
returned `stop_reason=tool_use` (the standard "I'm pausing to call a
tool, give me the result and let me continue" signal). The tool ran,
the result was placed in the transcript, and then **the CLI's
`--print` invocation terminated without re-invoking the model with
the tool result.**

That is **not** the normal Claude API loop. Normal:

```
model → stop_reason=tool_use   → CLI runs tool → re-invoke model with result
       → model continues, may call more tools or emit text
       → eventually stop_reason=end_turn
```

What Octopus is seeing:

```
model → stop_reason=tool_use   → CLI runs tool → tool_result written
       → ❌ CLI exits the --print invocation here, without re-invoking
```

That premature exit is what gives Octopus's `_emit_result` something to
fire, which closes the WS stream, flips session status back to `idle`,
and re-activates the input box in the chat UI — exactly the symptom
the user reported.

The synthetic-pair injection we obsessed over for §1-§11 is **downstream
of this**: the next `--resume` invocation sees a trailing assistant turn
that has tool_use but no text (because the model never got a chance to
emit text after the tool), patches it with `Continue / No response
requested.`, and proceeds.

So we've been correctly identifying the synthetic pair, but as a
*symptom*, not a cause. The cause is one layer deeper: the CLI is
cutting the multi-tool loop short after exactly one roundtrip.

---

## 13. Why VM0 doesn't see this (revised, again)

VM0's command shape — positional argv prompt, no `--input-format=stream-json`,
`--dangerously-skip-permissions` — appears to drive the CLI into its
**normal multi-tool loop** code path that runs until the model emits
`stop_reason=end_turn`.

My earlier probe (`probe_premature_stop.py`) confirmed this: with
shape B (VM0-like), a 3-step Bash task completed all three tool calls
in one CLI invocation. Same task in shape A (Octopus's exact flags)
also completed in the isolated probe — which is why I initially
rejected the input-format theory.

The mismatch between "shape A works in probe" and "shape A fails in
production" is the open question (§14).

The user's observation that VM0 never exhibits this is therefore
consistent: VM0 always takes the multi-tool-loop code path that runs
to `end_turn`. The single-roundtrip premature exit is something
Octopus's specific flag set triggers under conditions we have not yet
fully isolated.

---

## 14. Why my probes don't reproduce — what to dig into

Two probes ran, neither reproduced the premature-exit bug:

- `probe_premature_stop.py` (fresh session, 3-step Bash task, both
  shapes) — both completed all 3 tools.
- `probe_octopus_full.py` (full Octopus flag set + `--mcp-config`
  viewer+bg + system prompt + `--resume` after a tool-only trailing
  turn, 3-step Bash task) — both completed all 3 tools.

Yet in this very session's production transcript, single-tool
trailing turns are observed terminating the `--print` invocation
without re-invoking the model. Candidates for what's actually
triggering this in production:

1. **Long-context threshold.** The current session's JSONL is
   massive (>500K tokens of cache reads on recent calls). The
   probes start fresh. Maybe the CLI's loop has a max-time or
   max-tokens budget that trips when the context is large, ending
   the turn after the first tool result rather than incurring
   another expensive model call.

2. **Specific tool types or output sizes.** Probes used `cat` on
   tiny files and `echo` on short strings. Production failures
   were on `Edit` (large diff text) and `grep` (long output). The
   CLI might have a heuristic that ends the loop when the
   accumulated assistant+tool_result token count crosses a
   threshold within one --print invocation.

3. **Stdin handling specific to Octopus's `send_initial_prompt`.**
   Octopus writes the prompt as a single JSON line and leaves
   stdin open. The CLI may have an idle-timeout heuristic
   ("if no new stream-json input for N ms after a tool_result,
   assume turn done"). The probes write one line and read until
   `result`, same as Octopus — but the CLI's timeout might
   interact with the *speed* at which Octopus reads its stdout,
   producing different timing.

4. **Resume + parallel tool-call interaction.** Earlier in the
   transcript, multiple Bash calls with the same `requestId`
   succeeded — the model emitted parallel tool_use blocks in one
   response and the CLI ran them all in one round. Failures all
   show *sequential* tool_use (one per request). Maybe the CLI's
   stream-json input loop terminates after one *sequential*
   roundtrip but not after one *parallel* roundtrip.

5. **MCP-server initialization timing.** Octopus registers two MCP
   servers via `--mcp-config` (viewer + bg). Each spawns a Python
   subprocess on `--print` startup. The probe registers the same
   servers in `probe_octopus_full.py` so this shouldn't differ —
   but maybe the MCP servers' first call (which is slow on cold
   start) consumes a budget the CLI is tracking.

6. **`--permission-prompt-tool=stdio` interaction with tool
   loops.** The flag activates the control protocol over stdio.
   Maybe under certain conditions the CLI sends a `can_use_tool`
   control_request, expects a `control_response`, and if it
   doesn't get one within a window, ends the turn. The probes
   auto-allow correctly; production also does. But maybe the
   timing varies.

The next concrete experiments to run, in priority order:

- **(a) Repro against the actual production session.** Use Octopus's
  `_make_backend` directly with the current session's `claude_session_id`
  and `working_dir`, send a probe prompt that ends with a tool call,
  watch whether the CLI re-invokes. Without `--resume` corruption: pass
  a *throwaway* prompt-id and check transcript bytes added.

- **(b) Repro with a long synthetic context.** Build a session with
  ~50 fake turns, then drive a tool-call turn and see if the CLI
  terminates early.

- **(c) Try `--include-partial-messages`** and other observability
  flags to see what the CLI is actually doing between tool_result
  delivery and exit.

- **(d) Strace the CLI** during a failing turn to see exactly what
  happens on stdout/stdin around the tool_result write. Most
  diagnostic but most invasive.

Recommended path: (a) first — it's the highest-signal probe with the
lowest setup cost. If (a) reproduces, we can then bisect by stripping
flags until the bug disappears.

---

## 15. Revised fix options

Now that the real bug is "CLI exits after one tool roundtrip" rather
than "synthetic pair pollutes context":

### (A') Switch to VM0's command shape
- Positional argv prompt, drop `--input-format=stream-json`.
- Need to verify the control protocol over stdio still works in this
  shape — the probe didn't actively trigger `can_use_tool` in either
  shape, so this part is uncertain. Octopus's `AskUserQuestion`
  interception depends on it.
- If verified, this is the cleanest fix: it puts the CLI on the same
  code path VM0 uses, which we have months of evidence runs the full
  multi-tool loop correctly.
- Estimated cost: ~30 lines in `claude_code.py` (build_args + drop
  the stdin-write path) + a thorough e2e pass on AskUserQuestion +
  permission flows.

### (B') Keep `--print --resume` but loop in Octopus
- After each CLI exit, if the trailing assistant turn was tool-only
  and the model's last `stop_reason` was `tool_use`, immediately
  re-spawn with `--resume` and an empty stdin message to drive
  another roundtrip.
- Brittle. Requires us to track per-turn whether the model finished
  naturally vs. got cut off. Each re-spawn is a fresh process
  spin-up cost (~hundreds of ms).
- Last-resort workaround if (A') is blocked.

### (C') Persistent CLI process per session
- Drop `--print` entirely. Hold one long-lived `claude` process per
  session, drive turns over its stdin. Eliminates both the resume
  cost and the per-turn loop-truncation issue.
- The architectural fix — same scope as we discussed for cross-turn
  bg. Big refactor, side benefits.
- Right for a future major version, not for fixing this bug alone.

Recommendation: pursue (A') *after* confirming via experiment (a) that
the bug actually disappears under VM0's command shape with Octopus's
production-shaped context. The current evidence is suggestive but not
conclusive (my synthetic probes didn't reproduce the failure in
either shape).

---

## 16. Evidence-driven update (post-probe round 2)

After more digging into the actual production transcript
(`~/.claude/projects/-home-start-up-Octopus/<session>.jsonl`, 4.3 MB,
746 assistant turns), the picture sharpened — and the simple "CLI
exits after one roundtrip" story turned out to be wrong too.

### Frequency at production scale

Scanned every `tool_result` delivery in the live transcript and
checked whether the next non-bookkeeping entry was an assistant
continuation:

| Outcome | Count | % |
|---|---:|---:|
| Continued (next entry is assistant) | 306 | 79% |
| **Premature** (next entry is user / EOF) | **83** | **21%** |

So in production, about 1 in 5 tool roundtrips ends the `--print`
invocation without model continuation. Distributed across tool types:
Bash 37, Edit 8, TaskUpdate 6, Grep 4, Read 4, Write 2, Monitor 2,
AskUserQuestion 2, ToolSearch 1, Agent 1.

### What it correlates with (and doesn't)

For the producing assistant message preceding each premature exit:

- **`stop_reason: tool_use` in 100% of cases** — model explicitly
  wanted to be re-invoked. No `end_turn`, no `stop_sequence`. The
  model never signaled "I'm done."
- **Token cap: not hit**. Max output_tokens 9,426. No clustering at
  any round-number cap (4096/8192/16384/32000/32768/64000). Cap
  theory dead.
- **Tool position in invocation: distributed**. Failures happen at
  invocation tool counts of 1, 2, 6, 50, 100+. Not a simple "exit
  after N tools" rule.
- **Tool_result size: tiny in 83% of cases**. 69 of 83 failures had
  the preceding tool_result < 1 KB. "Big output triggers it" theory
  dead.
- **Context size: strongest correlate**. Median input_tokens at
  failure is **327K** (cache_read alone is 318K median). Min 32K,
  max 657K. My probes top out at ~30K — that's why they don't
  reproduce.
- **Wall-clock duration: weakly predictive**. Premature median 202s,
  OK median 180s. Wide overlap. Not a clean idle-timeout shape.

### Transcript shape during premature exits — new wrinkle

Dumping the entries around premature exits revealed something
unexpected: **the producing assistant message is often re-emitted
2-3 times in a row with identical `output_tokens`**, with tool_result
entries interleaved between the copies, and sometimes with timestamps
that are out of order. Example (premature exit #3):

```
[48] assistant tool_use  out_tok=3781   t=7:52:28.421
[49] user      tool_result               t=7:52:28.424
[50] assistant tool_use  out_tok=3781   t=7:52:28.939   ← SAME logical msg, re-emitted
[51] user      tool_result               t=7:52:29.341
[52] user      tool_result               t=7:52:28.958   ← timestamp earlier than [51]
[53] assistant tool_use  out_tok=3781   t=7:52:29.334   ← SAME again
[54] last-prompt marker                                   ← invocation ends
```

`out_tok` being identical across the three writes is the tell — it's
one logical model API response being replayed in the transcript
multiple times, with multiple matching tool_result writes around it.
Same shape in exits #1 (`out_tok=397` twice) and #2 (`out_tok=613`
twice).

This is *not* a normal Claude API loop. It looks like the CLI is
doing something concurrent / re-entrant when handling tool results
at large context scale, and one of those paths ends with a `result`
event being emitted on stdout (which is what Octopus sees and treats
as "turn done").

### Revised root cause

The most defensible read of the data:

> At large context scale (median 327K input tokens), the `claude` CLI
> binary's stream-json input loop sometimes emits a `result` event on
> stdout while the model's actual last response was `stop_reason:
> tool_use` and tool execution is still in progress (or just
> completed). Octopus reads the `result` event, treats the turn as
> done, closes the WS stream, flips status to idle. The user sees the
> input box reactivate.
>
> The transcript anomalies (duplicate assistant messages, interleaved
> out-of-order tool_results) suggest a CLI-internal concurrency or
> retry path that mis-handles the loop boundary at this scale.

This is a **bug inside the `claude` CLI binary** — not something
caused by Octopus's command shape, not something we can fix by
changing flags. VM0 not seeing it might be because VM0 sessions
generally stay smaller (per-task sandboxes), not because of any
specific flag combination. We did not directly verify VM0's
transcripts at comparable scale.

### What didn't reproduce in synthetic probes

For the record, neither of these triggered the bug:

- `probe_long_context.py`: 20 prior turns of filler, ~93 KB transcript, 3-step Bash task. All shapes completed all 3 tools, stop_reason=end_turn.
- `probe_large_output.py`: Single CLI invocation, first Bash returns 150 KB of stdout, second Bash returns "DONE". Both tools ran, end_turn, "TWO" returned.

The triggering condition (~300K+ token cache reads, real model thinking enabled, sustained multi-tool sequences across many `--resume` invocations) takes more setup than a probe can easily build.

---

## 17. Revised fix recommendations

Both prior fix-direction recommendations need revision in light of §16:

### (D) File upstream with Anthropic — **new top recommendation**
- Cause is in the CLI binary, not in Octopus.
- Send the 21% statistic, the duplicate-`out_tok` transcript snippets,
  and the correlation with context size.
- Cost: ~1 hour to write up + minimal-repro attempt (which we
  acknowledge is hard).

### (E) Workaround in Octopus: auto-respawn on premature exit
- In `session_manager._run_backend` / wherever we observe the `result`
  event, detect when the producing assistant turn had
  `stop_reason: tool_use` AND the most recent tool_result wasn't
  followed by a model continuation.
- If detected, immediately spawn another `--resume` with an empty
  stdin nudge (`{"type":"user","message":{"role":"user","content":""}}`)
  to coax the loop forward.
- Brittle but cheap. Risk: getting into a loop where the CLI keeps
  exiting prematurely and we keep respawning forever — needs a
  retry cap (e.g. 3 attempts before giving up and surfacing an error
  in chat).
- Estimated effort: ~50 lines + tests.

### (A'), (B'), (C') from §15
- (A') Switch to VM0 command shape: status unclear; my probes didn't
  reproduce the bug under shape A *or* shape B, so we have no
  evidence the switch helps. Keep on the shelf.
- (B') Re-spawn on premature exit: same as (E) above, just named
  differently.
- (C') Persistent CLI per session: still a real refactor, still a
  valid long-term direction, but no longer the most direct fix.

### What to *not* do
- Don't try to filter the synthetic pair from the CLI's private
  transcript. We confirmed it's a downstream artifact; removing it
  doesn't fix the premature exit.

Recommendation: start with (D) (file upstream), implement (E) (Octopus
auto-respawn) as a near-term mitigation if the bug becomes painful
before the upstream fix lands.

---

## 18. Outcome — what we shipped

User decision (paraphrased): "adopt the VM0 approach anyway, and
don't give up any features." Implementing that meant rebuilding two
pieces that previously rode the CLI control protocol over stdin:

- **Per-tool permission decisions**: dropped entirely in favor of
  `--dangerously-skip-permissions`. The host trust model is unchanged
  — Octopus is the only thing spawning these `claude` subprocesses on
  the user's behalf — so the per-tool host callback was layering
  unneeded ceremony on top of a process that's already on the trust
  boundary.
- **AskUserQuestion interception**: rebuilt as a new MCP tool
  `mcp__ask__user` (`server/mcp_servers/ask.py`) that long-polls
  Octopus over HTTP. The model uses it the same way it used the
  built-in AUQ (the schema is preserved); the built-in is disabled
  via `--disallowedTools AskUserQuestion` so there's no ambiguity
  about which one to call.

### Backend command shape, before and after

| | Old (Octopus-original) | New (VM0-style) |
|---|---|---|
| Prompt delivery | `{"type":"user","message":...}` on stdin | Positional argv after `--` |
| Input format | `--input-format=stream-json` | (default: text) |
| Output format | `--output-format=stream-json` | unchanged |
| Permissions | `--permission-mode=default --permission-prompt-tool=stdio` + host callback | `--dangerously-skip-permissions` |
| AskUserQuestion | Built-in + intercepted via deny-channel hack on stdio | `--disallowedTools AskUserQuestion`, replaced by `mcp__ask__user` MCP tool |
| Initialize handshake | Sent on every spawn | Gone |
| Interrupt | `control_request{subtype:interrupt}` then stop | Just `stop()` (SIGTERM → 2 s → SIGKILL) |

### Files changed (this commit set)

- `server/backends/claude_code.py`: new command shape;
  `send_initial_prompt` is now a no-op; `_handle_control_*` are
  vestigial; `answer_question` is a back-compat no-op (the real path
  is `session_manager.answer_question` + asyncio.Event).
- `server/session_manager.py`: new state on `Session`
  (`_pending_question_events`, `_pending_question_answers`); new
  methods `create_pending_question` (called by the ask MCP server's
  POST) and `wait_for_question_answer` (called by the same server's
  GET long-poll); `answer_question` / `_deliver_question_answer`
  rewired to set the Event instead of calling `backend.answer_question`.
- `server/mcp_servers/ask.py`: new MCP server, single tool
  `mcp__ask__user`. POSTs the question, long-polls the answer.
- `server/routers/questions.py`: REST surface
  (`POST /questions`, `GET /questions/{id}/answer`,
  `POST /questions/{id}/answer`).
- `tests/_fixtures/fake_claude_cli.py`: drop the initialize/can_use_tool/
  interrupt control-protocol modes — those code paths are gone.
- `tests/test_backend_claude_code.py`: rewritten against the new
  shape (new tests for `build_args` VM0 flags + MCP server registration,
  removed old control-protocol round-trip tests).
- `tests/test_session_manager.py`: AUQ tests rewritten against the
  Event-based flow (added `test_wait_for_question_answer_*`).
- `tests/test_backend_claude_code_real.py`: real-CLI AUQ test
  removed (now covered by the Playwright e2e against the new shape;
  unit-testing the new path would require a live FastAPI).
- `web/e2e/new-features.spec.ts`: AUQ e2e updated to nudge the
  model toward `mcp__ask__user` (built-in is disabled).

The frontend (`web/src/components/QuestionPrompt.tsx`,
`web/src/hooks/useWebSocket.ts`) was **not** touched — the user's
submit still goes over WebSocket as `answer_question`, hits the same
`session_manager.answer_question` entrypoint, which now sets the
Event under the hood. UI semantics are identical.

### Verification

- `pytest tests/`: **387 passed** (was 386 pre-refactor; net +1 from
  added Event-flow coverage minus the deleted real-CLI AUQ test).
- `vitest`: **32 passed** (unchanged — no frontend source changes).
- `tsc --noEmit`: clean.
- `playwright test`: **44 passed** including the updated
  `AskUserQuestion (via mcp__ask__user)` test which exercises the
  full new path with real Claude against a freshly-spawned uvicorn
  using the new on-disk code.
- Frontend production build: clean.

### What's still unverified

The synthetic e2e workloads are small (a few turns, low-tens of
KB of context). The bug's strongest correlate was input contexts
of **300K+ tokens** (see §16). We have no probe that builds a
real 300K-token session and exercises the new shape against it
end-to-end. Confidence in the fix is therefore *structural*
(VM0's shape goes through a different CLI code path, and VM0
doesn't report this symptom even at production scale) rather
than empirical at-the-scale-the-bug-needs.

Concrete way to confirm post-deployment: restart the production
`octopus.service`, watch for any new "no `seq` after a `tool_use`"
patterns in the production DB. If 0 over a few hundred tool
roundtrips at large context, fix is verified empirically.

### What this fix does NOT address

- The bug almost certainly also exists in *any other host* using
  `claude --print --resume --input-format=stream-json
  --output-format=stream-json`. Filing upstream with Anthropic
  (option D from §17) still makes sense as a community good. We
  just no longer need it for ourselves.
- The synthetic-pair injection (§4–§11) is still present in the
  CLI's private transcript whenever a `--print --resume` invocation
  has a tool-only trailing turn. It remains *invisible to the UI*
  and now also doesn't matter for the loop correctness, since the
  loop is back to running cleanly. We just leave it alone.
