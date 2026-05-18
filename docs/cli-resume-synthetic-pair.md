# Synthetic-Pair Injection on `claude --print --resume`

Investigation notes from a debugging session that started as "Claude went
silent after editing a file twice" and ended up tracing a quirk in how
Octopus invokes the `claude` CLI binary.

**Status**: known cause, no code change yet. Proposal at the bottom.
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

Owner / target: closed for now. Tracked as "known invisible artifact,
documented." Revisit only if a downstream symptom shows up.
