# Agent Router, Channels and the Ledger

> **Status:** in-progress — Design: an Agent becomes one continuous entity — a Ledger of all its work, a default session and Channels behind a Router, and hand-made sessions left direct and free.

> Today an Agent is a shared configuration over sessions that cannot see each
> other, and the user is the one routing work between them. This plan records
> the architecture DeepSeek's Harness team published for the same problem
> ("Super-agent", with its open-source prototype), and the design Archer settled
> on for Octopus after working through it: a **Ledger** of every Worker's work, a
> **default session** per Agent that — like every **Channel** — goes through a
> **Router**, and sessions the user creates by hand that stay direct,
> independent and cost nothing extra. Nothing is built yet.

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

So the user carries the Agent's continuity in their head. DeepSeek measures the
same ceiling: a person keeps 3 to 5 active sessions at once, so past that **the
limit on multi-task work is human attention, not the model**. In Archer's words,
the sessions' awareness of each other is weak, so the Agent is *名存实亡*.

---

## 2. Reference: DeepSeek Harness "Super-agent"

Two sources, read in full:

- the article *Super-agent 设计探索：迈向更自主的 Agent 系统*, DeepSeek Harness
  team, 2026-10-31 — an architecture discussion;
- its open-source prototype,
  [`deepseek-ai/dsh-superagent-prototype`](https://github.com/deepseek-ai/dsh-superagent-prototype)
  (MIT, research code): a proactive coding agent for one GitHub repository and,
  optionally, Feishu. Its `docs/000`–`007` describe the implementation.

### 2.1 The architecture (article)

Four properties: **continuous** (one Agent across IM, GitHub, mail),
**efficient** (several tasks in parallel, never blocked by the last command),
**proactive** (acts on environment events, quiet otherwise), **persistent**
(always on, self-recovering). A single ReAct loop cannot have all four.

Five modules:

1. **Two layers.** A resident **Router** takes every human interaction and holds
   the global context; it does not execute tasks, so a lighter model suffices.
   Context-isolated **Workers** do one task each with the strongest model, report
   to the Router, and never talk to humans directly.
2. **Event-driven.** Inputs are **Triggers** (IM, mail, review comments, CI). A
   deterministic **Fast Path** filter (sources, groups, allowlists) runs before an
   LLM **Slow Path**. Events of one PR / topic / mail chain form a **Thread**; a
   Worker **subscribes** to its Threads and their later events go straight to it,
   bypassing the Router. Context is managed as a cache (**Inline & Spill**, chain
   of recall by object IDs). **Speculative execution**: try in the background,
   deliver only if useful.
3. **Human-facing interaction.** Full duplex: long tools are async
   `start()`/`stop()`. Output fits the channel (IM replies ≤ 100 words).
4. **Separating compute, state and environment.** A stateless cloud loop, state
   in a database, a light daemon on the user's machine (BYOM).
5. **Watchdog.** A timer-driven agent session that checks infrastructure and
   whether each Worker is progressing or drifting.

Measured on synthetic load (~300 messages, ~20 work requests, 5 runs): p50 / p95
completion 12.4 / 15.7 min against p95 29.5 min for a single session with a
sub-agent tool — **structure beats prompting**. Subscription pass-through cut
Router decisions ~30% and cached-read tokens 37%. Router batching: from ~1 to ~40
decisions/min, request time 2.5 → 3.9 s, amortized time per decision 6.4 → 1.6 s.

### 2.2 How the prototype actually does it

**The Router is mostly code.** An `Engine` — synchronous, no I/O, the only writer
of Triggers, Threads, Tickets and Decisions — does intake, deduplication,
authorization, fan-out to subscriptions, lifecycle and retries. The LLM "main
Session" acts only at **decision points**: accept a request, attach it, ignore
it, stop work, reply. Everything is stored first and executed after commit, so a
stall delays and never loses.

**The Router is off the path for most messages.** A person's message on a Thread
a Ticket already follows goes to that Ticket's Worker, not to the Router; the
Worker decides whether it is in scope and hands back what is not. A person can
also write directly in a Worker's session. The Router handles *new* work.

**It is built for asynchronous channels.** One acknowledgement, silence while
working, a message when a result is ready or a decision is needed. Its
philosophy: *take as little human attention as possible*. Its metric is
completion time in minutes; it does not measure first-response latency, and the
2.5–4 s of a Router decision is invisible at that time scale.

**Router context.** Compaction is on (`compaction-basic`, plus a tool-result
pruner keeping head 4,096 + tail 1,024 chars of results over 8,192). It is safe
because **the state of record is in the database, not in the Router's context**:
the dispatch prompt says *"Check ticket summaries after compaction to avoid
duplicate work."* Growth is kept slow at the source:

- inputs arrive as compact cards with pointers only for truncated content;
- ordinary Worker progress stays on its Ticket, and notices are injected without
  waking the Router;
- routing notes are capped at 30 words, cross-Ticket notes and chat messages at
  100;
- the Router may not read transcripts, code or logs;
- a guard refuses sleeps and waits over 5 s;
- the per-request Policy section is placed last, so the prefix stays
  prompt-cached.

**How a Worker is created.** The Router calls one tool, `orchestrator_route`, with
`accept`: a title, a worktree name, an acknowledgement, and an optional ≤ 30-word
routing note. It never rewrites the request into a plan: *"workers derive
requirements from the original source."* The Engine then creates, in one
transaction, a Ticket, its Mandate (what authorized it), the first Assignment,
Subscriptions and the queued acknowledgement. A launch then:

1. reserves a worker slot — `maxWorkers`, plus one extra reserved for a person's
   message, so new work never blocks a human;
2. runs a prepare command once (typically `git worktree`), with a 10-minute
   limit;
3. creates session `worker-<ticket>` under a worker preset whose foreground
   commands move to the background after 5 s, so it takes new messages at its
   next step;
4. sends the original Trigger inline as the assignment.

One request is one Ticket is one Worker. A later message attaches to an existing
Ticket **only on an explicit reference** (its PR, issue, source message or
acknowledgement); topic similarity is not enough.

**How a Worker is reclaimed — three separate levels.**

- **Slot.** Released when a turn ends with nothing running in the background, or
  at once on a hand-off to a person (`ready`, a question, `blocked`, `failed`).
  Background jobs may run at most 30 minutes after a turn.
- **Ticket.** Ends by outcome: `ready` without a PR; `dropped` when nothing needs
  doing; when the PRs it opened are merged or closed; or a person's stop.
  Unread follow-ups go back to the Router.
- **Session, directory and subscriptions.** Kept. A later comment on the PR
  resumes the same Ticket in the same session, directory and branch. Restarts
  resume with a recovery note.

---

## 3. What Octopus already has

| Concept | In Octopus today | Gap |
|---|---|---|
| Full duplex, async tools | **Ahead**: `mcp__bg__run` returns at once and delivers the result as a new turn ([`turn-safety.md`](turn-safety.md)); steering and queue + interrupt ([`inline-steering.md`](inline-steering.md)) | — |
| Router + Workers | Agents own sessions; `ask_agent` delegation with continuation and result injection ([`agent-collaboration.md`](agent-collaboration.md)); native sub-agents ([`native-subagents.md`](native-subagents.md)) | No Router. **The cycle guard rejects an agent delegating to itself** (`delegations.py`: "target agent already in the caller chain"), so Router → own Worker is not expressible yet |
| A record of all work | Messages, bg tasks, delegations and schedule runs each have their own table; `fork_helpers.classify_side_effects` already extracts the paths an agent touched | No Agent-level view across them |
| Channels / Triggers | Schedules ([`scheduled-runs.md`](scheduled-runs.md)); bg results and agent replies injected as turns; GitHub / Gmail connectors as **outbound tools** only | No inbound events, Threads, subscriptions or filter |
| Long-term knowledge | Per-agent memory ([`memory.md`](memory.md)) | Per agent, model-written, not shared |
| Resource reclamation | Held CLI processes reaped after 10 min idle, transcript kept, `--resume` | Reclaimed by idle time, not by semantic points; no resume on an external event |

Octopus's agent loop is not a stateless API call but a **harness CLI session**
(Claude Code, Codex) holding its own transcript. The Router and the Workers are
therefore harness sessions too, and stay behind the harness boundary
([`harness-layer.md`](harness-layer.md)): a Router is an ordinary run with a
different `RunConfig` (tools, model, addendum), so Claude and Codex Agents get
the same design. The Router can run a cheaper model through the per-session
model override ([`composer-and-time.md`](composer-and-time.md)).

---

## 4. Principles

1. **The direct path costs nothing.** Most sessions are one independent piece of
   work. A session the user creates by hand gets no extra hop, no extra token,
   no extra concept. Every new capability costs only where it is used. This is
   enforced by tests, not intent (§7).
2. **State of record lives in the database; a model's context is a cache.** The
   Ledger is the truth; the Router's context can be dropped and rebuilt from it at
   any time.
3. **Code by default, the model at decision points.** Intake, filtering, fan-out,
   lifecycle, retries and Ledger writes are code. An LLM is called only where
   judgement is needed: whether to take work, where it goes, what to say.
4. **Take as little human attention as possible.** Few concepts, one
   acknowledgement, silence while working, a message only when a person must act
   or can use a result.

---

## 5. Design

### 5.1 The concepts

| Concept | What it is |
|---|---|
| **Ledger** | The Agent's record of all work by all its Workers. Written by the system. The truth §4.2 refers to |
| **Board** | The Ledger's current view: what is in flight, by whom, waiting on what |
| **Channel** | An input source and its way back: the default session, IM, GitHub, mail, schedules, webhooks |
| **Router** | One per Agent. Decides what Channel input becomes, and owns every reply on a Channel |
| **Worker** | A session doing one item of work: dispatched by the Router, or created by the user |
| **Ontology** | Distilled long-term knowledge (people, projects, decisions, facts), Obsidian-like. Separate from the Ledger, built later from it (§5.7) |

### 5.2 Two kinds of session

**The default session — talking to the Agent itself.** Every Agent has one, and
it is the Web **Channel**: messages go through the Router. You hand work over,
ask what is going on, have it keep track of several things at once. Clicking the
Agent in the sidebar opens it; it is the Agent's home, not one more row.

- Its conversation is stored in Octopus's database, not tied to one CLI
  transcript; the Router behind it can rotate (§5.5).
- It is asynchronous by nature. A question the Ledger answers ("what did you do
  today", "how is that PR") is answered directly. "Go do X" gets one
  acknowledgement, then a Worker, then a report. The UI says so: for hands-on,
  streaming work, open a session.

**Sessions the user creates by hand — independent and direct.** Exactly as today:
no Router in front, nothing pushed into their turns, no extra cost. They are
still Workers in the Ledger, recorded by the system like every other. Two
optional bridges:

- **The Router may leave a note** (§5.5). A note is not delivered mid-turn and
  does not wake an idle session. It waits in the session's inbox and is
  injected at the start of the user's next turn, as information with its
  provenance. It grants nothing and asks for nothing. It is shown in the UI,
  and a session can mute notes.
- **Hand-over in both directions.** A hand-made session can be given to the Agent
  ("follow this up"), becoming a Router-owned Worker; a Router-dispatched Worker
  can be taken over by the user.

### 5.3 The Ledger

One item per piece of work:

```
ledger_items: id, agent_id, user_id, title,
              origin (default_session | channel:<kind> | manual | schedule | delegation),
              origin_ref (trigger / thread / session), worker_session_id,
              status (queued | working | waiting | needs_person | done | dropped | stopped),
              outcome, summary, files_touched, threads (channel refs),
              started_at, last_activity_at, ended_at
ledger_events: item_id, kind, at, ref, text     -- turn started/ended, report, hand-off, note
```

- **Written by the system, from events**, never by a session's model as a duty:
  - turn start (the user's input) and turn end (the tail of the final reply), from
    the SessionManager broadcast bus — the pattern `DelegationManager` already
    uses;
  - files touched, from `fork_helpers.classify_side_effects`;
  - bg tasks, delegations and schedule runs, from their existing records;
  - Router decisions and Worker reports.
- **A one-line summary** may be generated asynchronously after a turn by a cheap
  model, off the session's critical path. The direct path does not wait on it.
- **Read by pulling, never by pushing**: the Router reads it to route;
  any session may call `ledger.list` / `ledger.read` / `sessions.read` when it
  needs to; the Agent overview page shows the Board; the Watchdog and, later,
  the Ontology distiller consume it. Nothing is injected into a turn by default.

### 5.4 Channels

A Channel adapter turns a source into **Events** and carries replies back:

```
Event: id, agent_id, channel, thread_key, actor, addressed (bool),
       content (inlined, capped), refs, received_at
```

- **Sources**, in order:
  1. the default session (§5.2);
  2. one external source — GitHub (issues, PRs, review comments, CI) or an IM
     (Feishu / Slack);
  3. then mail and webhooks;
  4. schedules may also become a Channel. Each fire is then routed and lands in
     the default session and the Ledger, instead of a fresh session per fire.
- **Addressing and tags.** A mention of the Agent (`@Octo`), a label on an issue,
  a reply to the Agent's message marks an Event `addressed`. Unaddressed Events
  follow a per-source policy — *ignore*, *observe* (Ledger only) or *candidate*
  — and silent observation is opt-in.
- **Threads and subscriptions.** A Worker subscribes to the Threads of its work;
  a subscribed Thread's later Events go to it directly, as queued or steered
  input — the Router is not involved. It hands back what is out of its scope.
- **Fast-Path filter in code** (allowlists, addressing, subscriptions) before the
  Router is ever asked.
- **Outbound** replies go back on the originating Channel, in its register: short
  in IM, structured in GitHub and mail.
- **Untrusted content.** Every Event not typed by the user is wrapped as data with
  provenance, never as instructions — prompt-injection defence in one place.

### 5.5 The Router

- **One per Agent**, behind its default session and its Channels. A held session
  on a lighter model; no Bash; tools: read the Ledger, `route` (accept / attach /
  ignore / stop, several candidates in one call), `reply`, `note`.
- **Forwards, does not rewrite.** A dispatched Worker receives the original Event
  inline and a routing note of at most 30 words. Requirements come from the
  source, not from the Router's paraphrase.
- **Attaches only on an explicit reference**; topic similarity makes new work.
- **Batching.** Events arriving while it thinks are merged into its next turn.
- **Notes to sessions** carry a fact from other work. They need an explicit link
  (the same file, PR or decision), are capped at 100 words and rate-limited.
- **Context.** Inputs are compact cards; Worker progress stays in the Ledger and
  only hand-offs reach the Router; it does not read transcripts.
  **Rotation instead of compaction:** because the Ledger is the truth, a fresh
  Router seeded with a Ledger snapshot replaces one whose context has grown.
  This needs no summarization call, and it happens when the Agent is idle, so no
  user waits on it. The default session's conversation is unaffected, since it is
  stored by Octopus.

### 5.6 Workers

- **Creation.** A Router `accept` creates a Ledger item and a Worker in one step,
  through a new **worker mode** of delegation: same Agent, child session marked
  Router-owned, exempt from the cross-agent cycle guard and still under the depth
  cap. Continuation reuses `delegation_id`; reports return through the existing
  injection path.
- **Workspace.** Work on a repository runs in its own checkout (`git worktree`)
  so parallel Workers do not collide; other work gets its own directory under the
  Agent's workspace.
- **Concurrency.** A per-Agent slot limit for Router-dispatched Workers, plus one
  slot reserved for a person's message, so new work never blocks a human.
  Hand-made sessions are not counted against it; they stay under the existing
  per-account turn cap.
- **Reclamation, three levels**, as in §2.2:
  - **Compute.** Freed when a turn ends with nothing in the background, and at
    once on a hand-off to a person. The held-process reaper's idle timeout stays
    as the backstop.
  - **Item.** Ends by outcome — `ready`, `dropped`, its PR merged or closed, or a
    stop.
  - **Session and subscriptions.** Kept, so an event on its Thread resumes the
    same item in the same session.
- **Visible and takeable.** Router-owned Workers appear under the Agent marked as
  such; the user can open one and talk to it directly, and the Router is told,
  not involved.

### 5.7 The Ontology (later, separate)

Distilled from the Ledger and transcripts by a background session, shared across
the user's Agents, with provenance back to the source message, timestamps, and
per-Agent opt-out. Because a lookup the model forgets to make is useless, recall
is not left to the model alone:

- **Gated auto-recall in code.** Entity and alias match plus embeddings; inject
  only above a threshold.
- **Must-check moments.** The first turn, and whenever a known entity is named.
- **A small catalogue** in the prompt.
- **A missed-recall loop.** The distiller notes entities a session discussed but
  did not recall, so the gate can be tuned.

It is personal Octopus's counterpart of Kraken's business Ontology. Its own plan
when its turn comes.

### 5.8 Watchdog

A schedule-driven session per Agent that reads the Board and each Worker's recent
activity, flags work that is stuck or drifting, and tells the Router.

---

## 6. Security

- Untrusted Event content is data with provenance, never instructions (§5.4).
- Silent observation is opt-in per source and visible in the Ledger.
- A Worker started from an external Event runs under the Agent's tool policy and
  no more. Destructive actions on externally triggered work can require the
  Router's or a person's confirmation.
- Proactive work starts in a record-only mode: the Ledger shows what the Agent
  *would* have done, until a person turns it on — as the prototype does.
- In a multi-tenant install a Channel credential belongs to one account and an
  Event never crosses accounts ([`multi-tenancy-gaps.md`](multi-tenancy-gaps.md)).

---

## 7. Order of work

Every step must keep the direct path unchanged. A hand-made session's
time-to-first-token and per-turn tokens are measured before and after each step
by a test that fails on regression.

1. **The Ledger.** Tables, event-driven writes, async summaries, `ledger.*` and
   `sessions.read` tools, the Agent overview page (the Board).
   *Done when* any session of an Agent can find out what its siblings are doing
   without being told, and a hand-made session's turns are unchanged.
2. **The default session and the Router.** The Web Channel, worker mode of
   delegation, Router-owned replies, batching, rotation, slots, reclamation.
   *Done when* one conversation with an Agent runs three tasks in parallel Workers
   and reports each back.
3. **Channels.** The adapter framework; one external source (GitHub or an IM);
   Threads, subscriptions, the Fast-Path filter, untrusted-content wrapping;
   optionally schedules as a Channel.
4. **Router notes and hand-over** between the Router and hand-made sessions.
5. **Watchdog.**
6. **Ontology**, in its own plan.

---

## 8. Open questions

1. The Router's model per backend, and Codex: a Codex run is one process per turn
   (`--resume`), so a held Router is a Claude-side optimization. Measure Codex's
   latency and cache behaviour as a Router.
2. Whether schedules move to being a Channel (§5.4), or stay as they are.
3. The cost of Ledger summaries at scale, and whether they are on by default.
4. Silent listening in shared IM groups: who may enable it.
5. The per-Agent Worker slot limit, and how it interacts with the per-account
   turn cap.
6. Inbound SaaS Channels pull against the PRD's N5 ("no general SaaS connector
   catalogue"); reconcile for the sources Kraken's Domain Agents need.

## 9. What this defers

- Router sharding: one Router per Agent until measured otherwise. DeepSeek's
  batching data says it scales further than expected.
- Agent-to-agent communication across installs or accounts.
- Compute / state / environment separation, owned by
  [`runtime-providers.md`](https://github.com/archeryue/Octopus/blob/runtime-provider/docs/plans/runtime-providers.md)
  (on the `runtime-provider` branch) and Octopus-Runtime.
