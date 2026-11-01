# Agent Router and Channels

> **Status:** in-progress — Design: an Agent becomes one continuous entity — Channels bring every input in, a Router per Agent holds the global view, sessions become its Workers.

> Today an Agent is a shared configuration over sessions that cannot see each
> other, and the user is the one routing work between them. This plan records
> the architecture DeepSeek's Harness team published for the same problem
> ("Super-agent"), and Archer's direction for Octopus: a **Channel / Bridge**
> layer that takes every input source, including tags, and a **Router** that
> has a global grasp of what the Agent is doing. Nothing is built yet.

---

## 1. The problem: an Agent in name only

An Octopus Agent owns its sessions, schedules and memory
([`agent-refactor.md`](agent-refactor.md)). What its sessions actually share
today is **configuration, not state**. From the code:

| Shared across an Agent's sessions | Where |
|---|---|
| Persona (system prompt), model, credential, MCP set, connectors, sub-agents | `RunConfig`, resolved per turn (`harness/run.py`) |
| A memory directory — and only a blurb telling the model to read `MEMORY.md` "at the START of a task" and write when it decides to | `assembly.render_memory_blurb` (`harness/assembly.py`) |

| **Not** shared | Consequence |
|---|---|
| Which sessions of this Agent are running, and on what | A new session starts blind to the work in flight next to it |
| What each one concluded, decided or is waiting on | Two sessions redo, or contradict, each other |
| Any tool to look at a sibling session | `mcp__ask_agent__list` shows only the caller's own delegations |
| One place where the Agent answers | Each session talks to the user directly; the user is the router |

So the user carries the Agent's continuity in their head: which session has
the context for X, re-explaining it in another, noticing that a schedule's run
and a chat are about the same thing. The DeepSeek article measures the same
ceiling — a person keeps 3 to 5 active sessions at once; past that, **the limit
on multi-task work is human attention, not the model**. The Agent abstraction is
meant to remove that burden and currently does not. Archer's words: the
sessions' awareness of each other is weak, so the Agent is *名存实亡*.

---

## 2. Reference: DeepSeek Harness, "Super-agent" (2026-10-31)

*Super-agent 设计探索：迈向更自主的 Agent 系统*, DeepSeek Harness team,
2026-10-31. An architecture discussion, not a product description; parts are
implemented in their `dsh-superagent-prototype`. Recorded here because it is
the closest published design to where Octopus needs to go.

### 2.1 Four properties

- **Continuous** — the user talks to it in IM, in GitHub comments, wherever they
  already work, and it feels like one Agent: approach A agreed in IM, changed to
  B in a PR comment, and it follows.
- **Efficient** — responsive, and runs several complex tasks in parallel without
  waiting for the previous command to finish.
- **Proactive** — watches environment events and moves on what is worth
  handling without being told; quiet when nothing is.
- **Persistent** — always on, in the user's real environment, recovering from
  failures by itself.

A single ReAct loop cannot have all four; it needs an architecture.

### 2.2 Five modules

1. **Two layers.** One resident **Router** takes every human interaction and
   holds the global context — the system's sequence point, coordinator and
   memory. It does not execute tasks (it may not even have Bash), so a lighter
   model is enough. **Workers** are context-isolated sessions with full tools
   and the strongest model, one task each; they report to the Router and never
   talk to humans directly. Sessions become an implementation detail, not the
   user's cognitive load — "letting the user manage sessions is inertia from
   the LLM API". The Router batches: events arriving during one inference are
   merged into the next, so one resident Router session scales further than
   expected.
2. **Event-driven.** Inputs are **Triggers** — IM messages, mail, calendar,
   review comments, CI results — not typed prompts.
   - *Two-level filter*: a deterministic **Fast Path** (source, group, user and
     bot allowlists: "respond only to explicit requests" / "candidate" /
     "ignore") before an LLM **Slow Path** (policies written as prompts).
   - *Routing by subscription*: the events of one PR, IM topic or mail chain
     form a **Thread**. Once the Router creates a Worker for a Thread, the Worker
     **subscribes** and later events go straight to it (Fast Path); the Router
     only handles unsubscribed events. Thread ↔ Worker is many-to-many, so
     Workers in the same PR see each other's events and resolve conflicts
     without talking directly.
   - *Context as a cache*: **Inline** what is likely needed (IDs, short
     excerpts, neighbouring messages), **Spill** the rest behind a re-readable
     reference. With IDs inlined everywhere, objects form a reference graph and
     the model walks it on demand — **Chain of Recall**.
   - *Speculative execution*: act in the background before being asked
     ("silently reproduce and fix" when a bug is discussed); deliver if useful,
     discard if not.
3. **Human-facing interaction.** *Full duplex*: long tools become async
   `start()` / `stop()`, results come back as new events, so the agent always
   accepts new input. *Multi-channel*: output fits the channel — IM is short and
   low-noise (a hard cap of 100 words per reply worked), GitHub and mail are
   structured and complete.
4. **Separating compute, state and environment.** The agent loop runs stateless
   in the cloud with session state in a database (compute/state); the execution
   environment stays where it is, reached through a light stateless **daemon**
   on the device (compute/environment) — Anthropic's "decoupling the brain from
   the hands". This supports **BYOM** (bring your own machine) and machines that
   come and go: tasks for a device run when it comes back online.
5. **Watchdog.** An independent agent session driven by a timer — an external
   control loop. Besides infrastructure failures it judges whether each Worker
   is making real progress or drifting from the goal, and steps in. Being an
   agent, it can be asked why something failed and can adjust the main
   system's prompts and trigger rules.

### 2.3 Their measurements (synthetic load; preliminary)

- ~300 messages, ~20 of them real work requests, 5 runs each. Super-agent task
  completion p50 / p95 **12.4 / 15.7 min**. Single session with a sub-agent
  tool and a neutral prompt: it never delegated, everything ran in the main
  session and kept being interrupted, p95 **29.5 min**. With "prefer
  delegating", it delegated in 4 of 5 runs and was still slower, with a long
  tail from the run where it judged the work "small". **Structure beats
  prompting.**
- Super-agent spends more cached-read tokens than a single session: more
  tokens for shorter and steadier completion.
- Removing subscription pass-through left completion time unchanged but
  raised Router decisions ~30% and cached-read tokens 23.2M → 31.8M
  (+37%).
- Router batching: from ~1 to ~40 decisions/min, mean batch 1.0 → 2.8, request
  time 2.5 → 3.9 s, amortized time per decision 6.4 → 1.6 s.

### 2.4 Their open problems

Router sharding and long-term memory beyond weeks; how humans work with an
agent once sessions are hidden (the chat window is path-dependent, not
necessary); context boundaries and agent-to-agent communication across
instances; and **security**: an event-driven agent reads untrusted input
continuously, and a compromised cloud loop controls every connected device.

---

## 3. What Octopus already has

| Super-agent module | In Octopus today | Gap |
|---|---|---|
| Full-duplex, async tools | **Ahead**: `mcp__bg__run` returns at once and delivers the result as a new turn ([`turn-safety.md`](turn-safety.md)); steering a running turn and queue + interrupt ([`inline-steering.md`](inline-steering.md)) | — |
| Router + Workers | Agents own sessions; `ask_agent` delegation with continuation (`delegation_id`) and result injection ([`agent-collaboration.md`](agent-collaboration.md)); native sub-agents ([`native-subagents.md`](native-subagents.md)) | No resident Router; the user picks sessions. **The cycle guard rejects an agent delegating to itself** (`delegations.py`: "target agent already in the caller chain"), so Router → own Worker is not expressible yet |
| Triggers | Schedules ([`scheduled-runs.md`](scheduled-runs.md)); bg results and agent replies injected as turns; GitHub / Gmail connectors — as **outbound tools** only | No inbound events, no Threads, no subscriptions, no filter |
| Context management | Oversize prompt spill (`large_prompts`); per-agent memory ([`memory.md`](memory.md)) | No object IDs to recall by; no chain of recall |
| Compute / state / environment | Designed on the `runtime-provider` branch: the Runtime interface ([`runtime-providers.md`](https://github.com/archeryue/Octopus/blob/runtime-provider/docs/plans/runtime-providers.md)) and Octopus-Runtime's owner-machine node — the in-VM agent *is* their daemon | Not built |
| Watchdog | The nightly SRE schedule is an agent checking the system | Nothing watches a Worker's progress or drift |

Octopus's difference from their design: the agent loop is not a stateless
call to a model API but a **harness CLI session** (Claude Code, Codex) running
in the tenant's runtime, holding its own transcript. Router and Workers are
therefore both harness sessions; the Router can run a cheaper model through the
per-session `/model` override ([`composer-and-time.md`](composer-and-time.md)).

---

## 4. Design

```
 Sources: web chat · IM (@Agent, tags) · GitHub · mail · schedules · webhooks
          · bg results · agent replies
                              │
 ┌──────────────────── Channel / Bridge ────────────────────┐
 │ one adapter per source → a normalized Event              │
 │ Fast-path filter: allowlists, addressed-or-not,          │
 │   subscribed Thread → straight to its Workers            │
 │ outbound: replies rendered for the channel               │
 └───────────────────────────┬──────────────────────────────┘
                             │ unsubscribed / addressed Events
 ┌──────────── Router — one resident session per Agent ─────┐
 │ sees: the Agent Board + memory + this batch of Events    │
 │ decides: answer / hand to a Worker / open a Worker /     │
 │   ignore; owns every reply to a human                    │
 └───────────────────────────┬──────────────────────────────┘
                             │ dispatch (worker mode of delegation)
 ┌──────────── Workers — today's sessions ──────────────────┐
 │ subscribe to Threads · report to the Router ·            │
 │ read and update the Agent Board                          │
 └──────────────────────────────────────────────────────────┘
          Agent Board: the Agent's shared, structured state
```

### 4.1 The Agent Board — global awareness first

The core of "a global grasp" is not the Router session; it is **state the
Router and every Worker can see**. Without it a Router is one more session with
a long memory, and a Worker it did not start is as blind as today.

A Board item, in the site database (it is metadata — consistent with session
history moving into the runtime):

```
board_items: id, agent_id, user_id, title, status (open|working|waiting|done|dropped),
             owner_session_id, threads (channel refs), summary, last_update_at,
             created_by (router|worker|user|system)
board_notes: item_id, author_session_id, text, created_at
```

- **Written** by the Router (opens, assigns, closes), by Workers through a tool
  (`board.update`, `board.note`), and by the system (a turn ending updates its
  owner item's `last_update_at`; a session archived drops its claim).
- **Read** in two ways: a bounded digest injected into every turn's system
  addendum ("what this Agent has in flight", newest first, capped), and tools
  for depth — `board.list`, `sessions.list` (siblings with their items and
  summaries), `sessions.read` (a sibling's recent transcript, spilled).
- The UI shows it as the Agent's page: what it is doing, for whom, and where.

This alone — before any Router exists — makes sibling sessions aware of each
other, and it is the substrate the Router decides on.

### 4.2 Channel / Bridge

A Channel adapter turns a source into **Events** and carries replies back:

```
Event: id, agent_id, channel, thread_key, actor, addressed (bool),
       content (inlined, capped), refs (ids to recall), received_at
```

- **Sources**: the Octopus web chat; schedules; bg-task results and agent
  replies (today special-cased in `bg_tasks.py` / `delegations.py`, refactored
  to be Channels); then GitHub (issues, PRs, review comments, CI), mail, IM
  (Feishu / Slack), generic webhooks.
- **Tags / addressing.** A mention of the Agent (`@Octo`), a label on an issue,
  a reply to the Agent's own message: the adapter marks the Event `addressed`.
  Addressed Events always reach the Router (or the subscribed Worker).
  Unaddressed ones follow the Agent's per-source policy: *ignore*, *observe*
  (on the Board, no turn), or *candidate* (Router decides). Silent listening
  is opt-in per source.
- **Threads.** `thread_key` is the PR, the IM topic, the mail chain. Workers
  subscribe; the Fast Path delivers a subscribed Thread's Events to its
  Workers' sessions directly, as queued or steered input.
- **Outbound.** The Router replies through the originating Channel, rendered
  for it: short in IM, structured in GitHub and mail, as today in the web chat.
- **Untrusted content.** Every Event from outside the user's own typing is
  wrapped as data with its provenance, never as instructions; this is where
  prompt-injection defence lives, once, for every source.

### 4.3 The Router

- **One resident session per Agent**, held (`STREAM_JSON`) so it answers fast,
  on a lighter model by default. It does not run tasks — no Bash; its tools are
  the Board, dispatch and replying.
- **Batching**: Events arriving while it thinks are merged into its next turn
  (the existing queue, coalesced into one input).
- **Dispatch to its own Workers** is a new *worker mode* of delegation: same
  agent, child session marked as a Worker, exempt from the cross-agent cycle
  guard but still under the depth cap. Continuation reuses `delegation_id`; the
  Worker's result returns to the Router through the existing injection path.
- **Owns every reply to a human.** Workers report; the Router decides what the
  human sees and where.

### 4.4 Workers

Today's sessions, unchanged in what they can do. Added: they subscribe to
Threads, read and update the Board, and in Router-driven work they report to
the Router rather than replying to a channel. **A user can still open any
Worker and talk to it directly** — observability and direct control are
Octopus's strengths and stay.

### 4.5 Watchdog

A schedule-driven session per Agent (or per install) that reads the Board and
each Worker's recent activity, flags work that is stuck or drifting, and tells
the Router — generalizing what the nightly SRE check already does by hand.

### 4.6 Across the harness boundary

Everything above is backend-neutral and lives outside `server/harness/`
([`harness-layer.md`](harness-layer.md)): Router and Workers are ordinary runs
with a different `RunConfig` (tools, model, addendum). Claude and Codex Agents
get the same architecture.

---

## 5. Order of work

Each step is complete and useful on its own.

1. **Agent Board + sibling awareness.** Tables, the per-turn digest, the
   `board.*` / `sessions.list` / `sessions.read` tools, the Agent page. Done
   when a second session of an Agent can say what the first one is doing
   without being told.
2. **Router per Agent, web chat as the first Channel.** Worker mode of
   delegation; Router-owned replies; batching. Done when a user can hold one
   conversation with an Agent that runs three tasks in parallel Workers and
   reports each back.
3. **Channel / Bridge framework.** Schedules, bg results and agent replies
   become Channels; GitHub inbound; Threads and subscriptions; the Fast-Path
   filter and per-source policies; untrusted-content wrapping.
4. **IM Channel** (Feishu or Slack first), tags and addressing.
5. **Watchdog.**
6. **Speculative execution**, per-source and opt-in.

---

## 6. Security

- Untrusted Event content is data with provenance, never instructions (§4.2).
- Silent observation is opt-in per source and visible on the Board.
- A Worker started from an external Event runs with the Agent's tool policy,
  not more; destructive tools on externally-triggered work can require the
  Router's — or a human's — confirmation.
- In a multi-tenant install a Channel credential belongs to one account; an
  Event never crosses accounts ([`multi-tenancy-gaps.md`](multi-tenancy-gaps.md)).

---

## 7. Open questions

1. **Router per Agent, or one per user** dispatching across Agents? This plan
   assumes per Agent, matching what an Agent is meant to be.
2. **Silent listening** in IM: default off; who may turn it on in a shared
   group?
3. **Cost.** More sessions and more cached reads; the DeepSeek numbers say
   subscriptions pay for themselves. To be measured on Octopus workloads.
4. **Long-term memory** beyond the Board and `MEMORY.md` once an Agent runs for
   months.
5. **UI**: how far the Agent page replaces the session list as the default view.
6. **Connector scope.** Inbound SaaS Channels pull against the PRD's N5 ("no
   general SaaS connector catalogue") — to be reconciled for the sources the
   Kraken Domain Agents need.

## 8. What this defers

- Router sharding — one resident Router per Agent is enough until measured
  otherwise (their batching data says it scales further than expected).
- Agent-to-agent communication across installs or accounts.
- Compute / state / environment separation — owned by
  [`runtime-providers.md`](https://github.com/archeryue/Octopus/blob/runtime-provider/docs/plans/runtime-providers.md) (on the `runtime-provider` branch) and Octopus-Runtime.
