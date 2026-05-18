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

## 7. What VM0 does (and why it doesn't hit the bug)

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

VM0 does **not** post-process or filter `~/.claude/projects/…jsonl`. It
just doesn't trigger the injection in the first place.

`--output-format=stream-json` is shared by both and is fine. The
synthetic-pair behavior is keyed on the **input** format.

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

### (B) Drop `--input-format=stream-json`, pass prompt as positional argv (VM0 shape) — *contingent on the probe in §11*
- **Cost:** ~20 lines in `claude_code.py::build_args` + small
  refactor of `send_initial_prompt` (no stdin write).
- **Catch:** only viable if the control protocol over stdin still
  works with `--input-format=text`. We don't know yet. Run the probe
  in §11 first.
- **Recommendation:** if the probe is green, this is the right fix —
  eliminates the bug entirely with no functionality loss.

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

## 11. Proposed probe before committing to (B)

Before we change `claude_code.py`, verify experimentally:

1. Spawn `claude --print --output-format stream-json --verbose --permission-mode=default --permission-prompt-tool=stdio --resume <id> -- "test prompt"` (i.e. our current flags minus `--input-format=stream-json`, prompt as positional).
2. Check that the initialize control_request handshake still completes.
3. Trigger a tool call that requires permission (e.g. `Bash`). Check
   that a `can_use_tool` control_request arrives on stdout.
4. Write a `control_response` with `{"behavior":"allow","updatedInput":{…}}` to stdin. Check the tool actually runs.
5. Trigger `AskUserQuestion`. Check we can intercept and answer it via
   the same path.
6. Verify the CLI's jsonl transcript for this run contains **no**
   synthetic `Continue / No response requested.` pair.

If all six pass → we ship (B). If the control protocol degrades, we
fall back to (A) and add the memory note.

Owner / target: TBD. Tracked for now as "future cleanup, no user-visible
impact"; revisit when we have a second reason to touch this layer.
