# Native sub-agents, surfaced

> **Implementation status: SHIPPED.** `SubagentUpdate` in
> `server/harness/events.py`, the `task_*` branch in the Claude parser and the
> `collab_tool_call` branch in the Codex one, `_record_subagent` +
> the broadcast in `server/session_manager.py`, `agents.subagents` →
> `--agents`, and `SubagentCard` in the UI. Tests: `tests/test_subagents.py`
> (16), `tests/test_subagents_real.py` (a live `Task` run and a custom
> `--agents` sub-agent answering by the name its Octopus agent gave it),
> `SubagentCard.test.tsx`, and an `@llm` e2e that watches a real sub-agent
> narrate itself in the chat.
>
> Both CLIs already spawn sub-agents inside a headless turn — this makes
> Octopus *show* them, and lets an Octopus agent define its own.

## 1. What's already true (measured, not assumed)

Neither CLI needs anything enabling. In Octopus's exact invocation:

* **Claude Code 2.1.274** — `Task` is in the tool list, and the `init` event
  advertises `agents: ['claude', 'Explore', 'general-purpose', 'Plan',
  'statusline-setup']`. A real run (Task → Explore, find a token in a
  directory) finished in 7.6s and streamed a full progress contract:

  | event | payload |
  |---|---|
  | `system/task_started` | `task_id`, `tool_use_id`, `description`, `subagent_type`, `prompt`, `spawn_depth`, `is_backgrounded` |
  | `system/task_progress` | `description` (what it's doing now), `last_tool_name`, `usage{total_tokens, tool_uses, duration_ms}` |
  | `system/task_updated` | `patch{status, end_time}` — **no `tool_use_id`** |
  | `system/task_notification` | `status`, `summary`, `output_file` |

* **Codex 0.132.0** — `spawn_agent` / `send_input` / `wait_agent` /
  `resume_agent` / `close_agent` (`multi_agent` is a stable feature). A real
  run emitted `collab_tool_call` items carrying `tool`, `prompt`,
  `sender_thread_id`, `receiver_thread_ids`, and `agents_states{thread_id:
  {status, message}}` — a child *thread*, with its own id and transcript.

What Octopus does with all of that today: nothing. It drops every `system`
subtype it doesn't know and every `collab_tool_call` item, so a sub-agent that
runs for four minutes is a tool chip that looks frozen, and the only visible
trace is the tool result when it's over.

## 2. A sub-agent is not a delegation

Octopus already has agent-to-agent work: a **delegation** is a real session,
owned by another agent, that outlives the turn and can be reopened, answered
and continued (`agent-collaboration.md`). A sub-agent is the opposite kind of
thing: ephemeral, anonymous, scoped to one tool call, gone when the turn ends.

So it gets its own vocabulary rather than being forced into sessions: an
**observation** attached to the parent tool call, with no row of its own.

## 3. One shape for both harnesses

```python
@dataclass
class SubagentUpdate:
    task_id: str            # the harness's own id for the run
    tool_use_id: str | None # the parent tool call it belongs to
    status: str             # running | completed | failed
    name: str               # "Explore", "general-purpose", a Codex thread id…
    description: str        # what it is doing right now
    prompt: str             # the brief (first observation only)
    summary: str            # final text (terminal observation)
    tokens / tool_uses / duration_ms
```

`HarnessEvent` gains `type="subagent"` carrying one of these. Claude's four
`task_*` subtypes and Codex's `collab_tool_call` both normalize onto it, which
is the whole point of the harness layer: the UI never learns which CLI it is
talking to.

The Claude parser keeps a `task_id → (tool_use_id, name)` map for the life of
the run, because `task_updated` arrives with only a `task_id` — the one event
that can't identify itself.

## 4. Progress is broadcast, state is remembered

Progress events are **not persisted**, for the same reason token deltas
aren't: they're a live view of something whose durable record already exists
(the `Task` tool call and its result). Persisting them would double every
sub-agent in the transcript.

But a browser reload mid-run must not blank the card, so the session keeps the
last observation per `tool_use_id` in memory (bounded, oldest dropped) and the
session snapshot (`GET /api/sessions/{id}`) carries them. A server restart
loses them — correctly: the restart killed the CLI process, so the sub-agent
is gone too.

## 5. The card

**Keyed on the spawning tool call's id, never on the tool's name.** The name
is not stable: this CLI version advertises `Task` in its init event and then
emits the call as `Agent`, and Codex's is `subagent:<tool>`. A card that
matched names would have quietly stopped rendering on a CLI upgrade — the
worst failure mode available, because everything else would keep working.
Every tool call mounts a card; the ones with no sub-agent behind them render
nothing.

Rendered where the spawning tool call sits in the transcript: the sub-agent's name, what it's doing right now, its token and
tool counts as they climb, elapsed time, and — when it lands — the summary,
collapsed to a few lines with the full text one click away. Failure says so
with its reason rather than a chip that simply stops moving.

## 6. Agents that bring their own sub-agents

`--agents '<json>'` registers session-scoped sub-agents on the Claude CLI:
`{"reviewer": {"description": "…", "prompt": "…", "tools": […], "model": "…"}}`.
That maps exactly onto an Octopus agent's configuration, so an agent row gains
a `subagents` JSON column and the agent form gains an editor for it. An agent
that defines none behaves exactly as today — the CLI's built-ins are still
there.

Codex has no equivalent in `exec` (its `child_agents_md` feature is
unreleased), so its sub-agents stay the built-in kind; §9.

## 7. Work that outlives its turn

Claude Code decides on its own to run some sub-agents **asynchronously**. The
tool returns `Async agent launched successfully` immediately, the parent turn
ends, and the sub-agent keeps working inside the held process. When it lands,
the CLI wakes the agent to report it — a whole turn's worth of text nobody
asked for at that moment.

Octopus used to drop every one of those events. `HarnessRun` closes its stream
at the turn's `result`, and everything after that was discarded, so:

* the card spun forever, because the sub-agent's completion never arrived;
* **the answer never arrived either** — the agent's follow-up report went to
  the floor;
* and if the user happened to send the next message first, the async
  sub-agent's events landed in *that* turn's stream instead, attributed to
  whatever was open.

So a held run now has an **idle handler**: events that arrive with no turn in
flight go to the session rather than nowhere. They're handled exactly like
in-turn events minus the turn machinery — persisted if they map to a message,
broadcast either way.

Session status deliberately stays `idle` through all of this: there is no
`_active_task` to interrupt, and showing "running" would offer a stop button
that stops nothing.

The other half of the same problem: a sub-agent lives *inside* the CLI
process, so when the idle reaper stops it (or a restart does), anything still
marked running is over. Releasing a process closes out its runs — a card that
keeps spinning after its process is gone is a lie.

This is the same hole native cron ticks fall through, and the same fix covers
them.

## 8. Cost

A sub-agent spends tokens on its own conversation, and `task_progress` reports
them. The card shows the running total, and the turn's own `result` cost
already includes the sub-agent's spend, so nothing about billing changes — it
just stops being invisible.

## 9. Testing

Parser snapshots for both harnesses (every event, including the anonymous
`task_updated`), the session-manager path (broadcast-only, snapshot-carried,
bounded), the out-of-turn path (§7: post-turn events persisted and broadcast,
runs closed out when the process goes, an event for a deleted session
ignored), `--agents` argv rendering, the card's states in the UI, and
real-CLI tests that drive an actual `Task` through the harness, register a
sub-agent an Octopus agent defined, and — the regression that prompted §7 —
let an **asynchronous** sub-agent finish after its turn has ended, asserting
both its completion and the agent's follow-up report survive.

## 10. What this defers

* **Custom sub-agent definitions for Codex** — no CLI surface for it in
  `exec` mode today (`child_agents_md` is unreleased). The Codex app-server
  may expose it; that's the `harness-transport` question, not this one.
* **Opening a sub-agent's own transcript.** Claude writes one under the
  session's `tasks/` dir and Codex's child threads are real threads on disk;
  reading them is a file-tailing feature, not an event-stream one.
* **Steering a running sub-agent** (Codex's `send_input`, Claude's
  backgrounded tasks). The model can already do it from inside the turn; a
  human doing it from the UI needs a target selector and a policy for what
  happens when the parent turn ends first.
