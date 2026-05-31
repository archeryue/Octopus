# Agent Collaboration — Tech Plan (agent-to-agent delegation)

> **Post-implementation refresh.** This document now describes the
> shipped feature; see commit history for the review-driven fixes that
> closed the plan/implementation drift.

## 0. What we're building, and the mental model

Today an Octopus session has one human on the outside and one agent on
the inside. We want **agents to call each other**: from inside Octo's
session you say "ask Vera to review the code", Octo dispatches the
request to Vera, Vera works in her own session with her own
configuration, and Vera's eventual reply lands back in Octo's session
as a fresh turn. Octo and the user keep talking while Vera works;
multiple delegations can be in flight at once; Vera can in turn
delegate to Pete.

The whole feature collapses onto **one mental model**: every session
has exactly one **caller**.

- Root sessions (started by you) → caller is the **human**.
- Delegation sessions → caller is the **parent agent's session**.

Two universal rules govern every interaction:

1. **Questions always travel one hop, up the chain to the caller.**
2. **Replies always travel one hop, up the chain to the caller.**

That's the whole feature. It means:

- The existing `ask` MCP tool ("ask the human") generalises to "ask my
  caller". For root sessions the caller happens to be the human; for
  delegation sessions the caller is the parent agent's model.
- The `ask_agent` MCP server's `ask` tool is the inverse: "become a
  caller — spawn a child session under another agent and wait for it
  to talk back".
- A parent agent that receives a delegated question decides, in plain
  language, whether to answer it directly, ask its *own* caller
  (recursing one hop up), or fail the delegation. No special-case
  cross-session UX; it's just the model's normal tool-use loop.

We piggy-back on Octopus's existing **bg-task pattern** for the
async plumbing. `mcp__ask_agent__ask` returns a delegation id
immediately, the agent's turn ends, and when the child session
produces a reply (or question, or terminal error) Octopus injects a
follow-up turn into the parent session. No new long-poll
infrastructure, no blocking subprocesses, parallel fan-out for free.

This plan reverses the explicit "no A2A" carve-out from
`agent-refactor.md` §40-41 / §304. That carve-out conditioned A2A on
a Run table or a "trigger mechanism". Neither turned out to be
needed: a delegation is a normal `Session` row with a
`parent_session_id`, and the trigger is just an MCP tool call.

## 1. Goals

- A built-in MCP tool `mcp__ask_agent__ask(name, request, files?)`
  available to every agent by default, by which an agent can delegate
  a request to another named agent. The Python function that backs it
  is still named `ask_agent`; the exported MCP tool name is `ask`
  via `@mcp.tool(name="ask")`.
- Delegations are **async, bg-task-style**: tool returns immediately
  with a `delegation_id`; the parent agent's turn ends; the child's
  reply arrives later as an injected follow-up turn.
- Delegated sessions are **first-class Sessions** under the target
  agent — same harness, same MCP set, same credentials, same memory
  dir, same UI rendering as a normal session — distinguished only by
  `origin="delegation"` and a `parent_session_id` pointer.
- Mid-flight **questions from the child travel back to the parent**
  the same way replies do (turn injection). The parent decides what
  to do with them. The existing `ask` MCP tool is made caller-aware
  so this works without inventing a second question pipeline.
- Multiple concurrent sessions per agent — the target agent can be
  serving several delegations (or the user, or other agents) at once.
- Cancellation, listing, and recursive nested delegation, all with
  cycle/depth guards.

## 2. Non-goals

- **No synchronous mode.** The MCP tool never blocks. (Considered
  briefly; bg-task-style is strictly better — parallel fan-out, the
  user can keep talking, the same plumbing handles everything.)
- **No new top-level "delegation" table.** A delegation *is* a child
  Session row. The session id is the delegation id. We do not invent
  a parallel id space.
- **No new `Run` table.** Same reasoning as `agent-refactor.md`: a
  turn stays implicit inside a session. The async wakeup mechanism
  already exists for bg-tasks; we reuse it.
- **No automatic context-sharing from parent to child.** Vera does
  not see Octo's transcript. The model paraphrases what Vera needs
  into the `request` argument and names files explicitly via
  `files=[…]`. This is a deliberate privacy/token/leak boundary.
- **No agent permissioning in v1.** Any agent can ask any other.
  Per-agent allow/deny lists ("Vera is askable by: [Octo, Pete]") are
  a future feature; the schema is forward-compatible.
- **No special multi-agent UI dashboard.** The chat view renders
  delegations inline as request/event cards over the existing message
  stream; that's it. The user can always click through into the child
  session for the full transcript.

## 3. Reference: the bg-task pattern as template

`server/mcp_servers/bg.py` is the closest existing thing and we want
to mimic its shape almost exactly:

| bg-task | agent-delegation |
|---|---|
| `mcp__bg__run(command, description?)` | `mcp__ask_agent__ask(name, request, files?)` |
| Returns `task_id` immediately | Returns `delegation_id` immediately |
| Parent turn ends | Parent turn ends |
| Subprocess runs in background | Child Session runs in background |
| Captured stdout/stderr | Captured `assistant_text` events |
| On completion: inject follow-up turn `[bg-task-result …]` | On completion: inject follow-up turn `[agent-reply:<name> …]` |
| `mcp__bg__cancel(task_id)` SIGTERM | `mcp__ask_agent__cancel(delegation_id)` stops the child session and its running descendants |
| `mcp__bg__list()` recent tasks | `mcp__ask_agent__list()` recent delegations |

The follow-up-turn injection mechanism (`SessionManager.start_message`
with structured `[agent-…]` content, the same queued wakeup property
bg relies on) is the load-bearing piece we reuse for **three** kinds
of inbound events from the child:

- `[agent-reply:<name> delegation=<id>] <text>` — terminal success
- `[agent-question:<name> delegation=<id> question_id=<qid>] <text>` — child
  needs an answer to proceed
- `[agent-error:<name> delegation=<id> reason=…] <text>` — terminal
  failure (cancelled, hit error, exceeded budget)

Each shape carries the `delegation` id so the parent's model can
disambiguate when multiple delegations are in flight.

## 4. Data model changes (small)

### 4.1 `sessions` — two columns

```sql
ALTER TABLE sessions ADD COLUMN parent_session_id TEXT
  REFERENCES sessions(id) ON DELETE SET NULL;
-- origin enum already exists ('user' | 'schedule' | 'bridge'); extend
-- the API-side validator to also accept 'delegation'. No DDL — the
-- column is plain TEXT.
```

A delegation session has `parent_session_id` set and
`origin='delegation'`. The id of that row **is** the delegation id —
no separate id space. `ON DELETE SET NULL` (not CASCADE) is
deliberate: if the parent is hard-deleted we orphan the child rather
than mass-delete; sessions are precious. Listing/cancellation/UI
treat an orphaned delegation as a normal archived session.

### 4.2 `pending_agent_questions` — derive from existing AskQuestion

The existing `ask` MCP server already maintains an in-process
"session has a pending question" queue (per `Session._pending_questions`
in the inventory). We add **no new table**: a question from a child
session is stored in that same in-memory queue *and* routed to the
parent. The parent's `mcp__ask_agent__answer` tool call posts the
answer back into that queue, identical to how a human UI click does.

What changes is the *delivery side*, not the storage: when an `ask`
call lands on a session with `parent_session_id != null`, instead of
(only) pushing it to the websocket for the human UI to render, the
server *also* injects an `[agent-question:…]` turn into the parent
session. Either answer source (parent agent's tool call, or — if the
child happens to be a root session — a human click) drains the same
pending entry.

### 4.3 Optional `sessions.delegation_request` for display

```sql
ALTER TABLE sessions ADD COLUMN delegation_request TEXT;
```

When `origin='delegation'`, store the original prompt verbatim so the
UI can render "Octo asked: «…»" at the top of Vera's session view
without rummaging through the first message. NULL for non-delegation
sessions. Nice-to-have, not load-bearing — the same string is also the
first user-message in the child's transcript.

## 5. Behavior

### 5.1 `mcp__ask_agent__ask(name, request, files?)` — invocation

A new built-in MCP stdio server, `server/mcp_servers/ask_agent.py`,
shaped exactly like `bg.py`. Four MCP-exposed tools: `ask` (start),
`answer` (answer a child question), `cancel` (stop), and `list`
(introspect). Their full mounted names are
`mcp__ask_agent__ask`, `mcp__ask_agent__answer`,
`mcp__ask_agent__cancel`, and `mcp__ask_agent__list`. The underlying
Python functions keep the longer names
`ask_agent` / `answer_agent_question` / `cancel_agent_task` /
`list_agent_tasks`; the decorators expose the short MCP names
(`server/mcp_servers/ask_agent.py:3`). Env injection
(`OCTOPUS_API_BASE`, `OCTOPUS_AUTH_TOKEN`, `OCTOPUS_SESSION_ID`)
matches the existing pattern; the server is a thin HTTP shim to
FastAPI routes.

The server is added to the **default built-in set** in
`server/agent_manager.py` next to `ask`, `bg`. Existing agents created
before this change pick it up automatically (the default set is
applied on each turn assembly, not stored per agent), so no
migration. Agents that explicitly enumerate their MCP set will not
get it until the user adds it — same policy as other built-ins.

`POST /api/sessions/{parent_sid}/delegations` body:

```json
{
  "agent_name": "vera",
  "request": "review the latest commit on the dashboard branch",
  "files": ["web/src/components/Dashboard.tsx"]    // optional
}
```

Server-side flow:

1. **Resolve target agent.** Case-insensitive match on `agents.name`
   among non-archived agents. Ambiguous → 409 with the candidate
   list. Missing → 404. Cannot resolve to the same agent as the
   parent session's agent → 409 (self-delegation is almost certainly
   a mistake; if we ever want it, drop the check).
2. **Cycle + depth guards.** Walk `parent_session_id` upwards from
   the parent. Reject if any ancestor session belongs to the target
   agent (cycle). Reject if depth would exceed **3** (Octo → Vera →
   Pete is fine; Pete → Q is not).
3. **Create the child session** under the target agent:
   - `agent_id = target_agent.id`
   - `parent_session_id = parent_sid`
   - `origin = 'delegation'`
   - `working_dir = parent.working_dir` (inherits — see 5.7)
   - `name = f"{target_agent.name} ← {parent_agent.name}"`
     (auto-renamable later; just a sensible default)
   - `delegation_request = request`
4. **Compose the first user message** of the child session:

   ```
   You were asked by agent **{parent_agent.name}** (session {parent_sid}).
   Their request follows.

   ---
   {request}
   ```

   plus a trailing block listing `files` (if any) with their resolved
   absolute paths. The child agent sees this as a normal user-message
   — no custom message type, no special framing the model has to
   learn.
5. **Kick off the child's first turn** via the normal
   `SessionManager.start_message(...)` path. The already-registered
   delegation broadcast listener (see 5.3) observes the child's
   events.
6. **Return immediately** `{delegation_id: child_session.id, status:
   "started"}`. The MCP shim translates this to a short text the
   model can quote: "Started delegation `<id>` to Vera. I'll receive
   her reply as a follow-up turn."

### 5.2 Sub-session lifecycle

A delegation child session is an ordinary session for every purpose
except two:

- **Auto-archive after terminal delivery.** Delegation sessions are
  eligible for auto-archive, but the generic idle hook does not
  archive them. Archival happens in
  `DelegationManager._inject_terminal` after the terminal
  `[agent-reply:…]` or `[agent-error:…]` turn has been delivered (or
  attempted) to the parent (`server/delegations.py:739`,
  `server/delegations.py:788`). This is load-bearing for nested
  chains: Vera in Octo → Vera → Pete is idle while waiting for Pete,
  but must remain live so Pete's terminal turn can reach her. The
  session is not auto-deleted — the user can still browse it from
  Vera's archived-sessions list.
- **No bridge fan-out.** Bridges only broadcast to `origin='user'` (and
  maybe `bridge`) sessions; a delegation must not also notify the
  user's Telegram chat. Single line in `BridgeManager._on_broadcast`.

Otherwise: same harness, same MCP set, same credentials, same memory
dir as Vera's other sessions. Memory writes from concurrent
delegations to the same agent share the agent's memory dir — see 5.8
on concurrency. The split is reflected in `SessionManager`:
`_AUTO_ARCHIVE_ORIGINS` is only `("schedule",)`, while
`_AUTO_ARCHIVE_ELIGIBLE` includes `"delegation"` for explicit
post-terminal archival (`server/session_manager.py:416`).

### 5.3 Reply delivery: the delegation listener

For each live delegation, the FastAPI process holds a small in-memory
record `DelegationRunState` keyed by child session id:

- `parent_session_id`
- `target_agent_name` (cached so we can format the injection prefix)
- `captured_text: list[str]`
- `state: "running" | "completed" | "failed" | "cancelled"`
- `_terminal_injected: bool` — idempotency guard for terminal delivery

The manager subscribes once to the `SessionManager` broadcast bus and
filters for tracked child session ids. It captures the same
high-signal stream the bridge cares about, with delegation-specific
terminal and question handling: `assistant_text`, `question_request`,
`result`, and `error` (`server/delegations.py:571`). Behavior per
event kind:

- `assistant_text` → append chunk to `captured_text`.
- `question_request` → inject `[agent-question:…]` into the parent;
  see 5.4.
- `result` (terminal) → finalise. Build the injection string:

  ```
  [agent-reply:vera delegation=ab12cd34ef56]
  <joined captured_text>
  ```

  Call `SessionManager.start_message(parent_session_id, text)` so the
  parent receives a fresh turn through the same queued path bg-task
  delivery uses. Mark `state="completed"` and keep the record in the
  in-memory registry so `mcp__ask_agent__list` can still see recent
  terminal delegations.
- `error` → finalise with `[agent-error:…]`, same injection path.
  `_terminal_injected` ensures that races between `result`, `error`,
  and cancellation still yield one parent turn.

Other tool approval requests (e.g. dangerous shell command) stay in
the child session — they're the *agent's* policy, not the user's. If
Vera's tool policy needs a yes/no the user has to open Vera's session
and decide. Bubbling tool approvals would obliterate the "questions go
through the model" rule.

### 5.4 Question delivery: caller-aware `ask`

Today the `ask` MCP server's flow is roughly:

```
ask.tool(question, options) -- HTTP --> POST /sessions/{sid}/questions
                                         pending_questions.put(...)
                                         broadcast to UI websocket
                                         wait for answer
                                         return answer to model
```

The design intent was one extra caller-aware step after putting the
question on `pending_questions` and broadcasting to the websocket:

```python
if session.parent_session_id is not None:
    parent_agent = ...
    injection = (
        f"[agent-question:{this_agent.name} "
        f"delegation={session.id} question_id={question_id}]\n"
        f"{question}"
    )
    SessionManager.start_message(session.parent_session_id, injection)
```

The pending question already has a unique id; nothing else changes.
The shipped implementation keeps that behavior but performs the parent
injection from
`DelegationManager._inject_question` after the child's
`question_request` broadcast is observed. The pending question remains
on the child session; the injected prompt names the actual exported
tool, `mcp__ask_agent__answer(delegation_id, choice)`, and also points
to `mcp__ask__user` and `mcp__ask_agent__cancel` as the escalation and
failure paths (`server/delegations.py:610`).

### 5.5 `mcp__ask_agent__answer(delegation_id, choice)` — closing the loop

A new MCP tool on the `ask_agent` server. Body posts to
`POST /api/sessions/{parent_sid}/delegations/{child_sid}/answer` with
the chosen option label (or freeform text if the question allowed it).

Server-side: `DelegationManager.answer_pending_question` drains the
oldest pending entry on the child by calling the same
`SessionManager.answer_question` path used by the UI. The model is the
parent agent; the websocket UI is unaware. Both producers (human
click, parent tool call) compete to drain the same queue; first one
wins. If both happen, the second one 409s. Multi-question batches are
rare; as shipped, the parent's `choice` applies to the first question
and the remainder are padded with empty selections
(`server/delegations.py:675`).

If the parent's model decides it can't answer, it has three options
expressible in its normal language/tool loop:

- Call its **own** `ask` tool to ask its caller (recursing one hop
  up). When the answer comes back, call `mcp__ask_agent__answer` with
  it. This is the "I don't know — let me ask the user" path.
- Call `mcp__ask_agent__cancel(delegation_id, reason="…")`. The child gets
  an exception in its `ask` call and finalises via the error path.
- Do nothing. The child stays waiting indefinitely. The user can
  open the child session and answer in the UI as the manual fallback.

### 5.6 `mcp__ask_agent__cancel` and `mcp__ask_agent__list`

`mcp__ask_agent__cancel(delegation_id, reason?)` →
`POST /api/sessions/{parent_sid}/delegations/{child_sid}/cancel`.
Server flips the record to `state="cancelled"` before interrupting the
child so the interrupt's own `error` broadcast cannot produce a second
terminal injection. Then it cascade-cancels running descendants whose
`parent_session_id` chain leads to the cancelled delegation, walking
`DelegationManager._records` breadth-first and recursing through the
same public cancel path for each descendant. Each cancelled record gets
the same state-flip-before-interrupt + terminal-inject treatment; the
`_terminal_injected` flag preserves single-inject idempotency across
races (`server/delegations.py:256`, `server/delegations.py:310`,
`server/delegations.py:749`).

The parent gets an injected
`[agent-error:vera delegation=… reason=cancelled]`. Descendant
terminal turns are delivered to their immediate parents before the
root cancellation is injected upward, so the one-hop invariant remains
true while the chain unwinds.

`mcp__ask_agent__list()` → `GET /api/sessions/{parent_sid}/delegations`,
returns the live + recently-finished `DelegationRunState` records for
this parent, capped at 25, most recent first. Symmetric with
`mcp__bg__list`.

### 5.6a Continuing a prior delegation — bimodal `ask`

The `mcp__ask_agent__ask` tool is **bimodal**. Either:

- Pass `name` to start a fresh delegation under a target agent (the
  shape described in §5.1), or
- Pass `delegation_id` (from a prior reply) to continue that
  delegation in the **same child session** — the target agent's
  in-session transcript carries across rounds.

Exactly one of (`name`, `delegation_id`) must be set; the server
rejects both-or-neither. The continuation routes to a separate
REST endpoint
(`POST /api/sessions/{parent_sid}/delegations/{delegation_id}/follow-up`)
but at the model's surface it's the same tool, distinguished by
which id is present.

The intended use of mode 2 is review/iteration loops: Octo asks
Vera, Vera replies + auto-archives; on the next round Octo calls
`ask` again with the previous `delegation_id` and Vera resumes with
her previous reply still visible in her own conversation — no
re-reading of files, no re-establishing context. Mode 1 is still
the right shape for **fresh / unrelated work**, and for **parallel
fan-out to the same target** (multiple in-flight delegations to one
agent need separate sessions to run concurrently — sharing one
would serialise them).

This bimodal merge was chosen over a separate `follow_up` tool
deliberately: every extra MCP tool costs per-turn system-prompt
context tokens AND gives the model an unnecessary "which one?"
decision. The plain `bg_run` pattern is the precedent — one tool,
parameters distinguish behaviour.

Server-side flow:

1. Validate the delegation belongs to this parent (404 if not), and
   is in a terminal state (409 if `running` — wait for the reply
   first, there is no sound semantic for "follow up mid-flight").
2. Unarchive the child session if needed. The round-2 auto-archive
   timing means *every* completed delegation child is archived by
   the time we get here; `SessionManager.unarchive_session`
   reloads the row (including `parent_session_id` and
   `delegation_request`, restored in round 3) and registers it in
   the live sessions map. A hard-deleted child can't be reused —
   409 with a "start a fresh delegation" hint.
3. Round-reset the in-memory `DelegationRunState`:
   `state="running"`, `captured_text=[]`, `error=None`,
   `finished_at=None`, `_terminal_injected=False`,
   `request=<new>`. Identity (`delegation_id`,
   `parent_session_id`, `target_agent_id`, `target_agent_name`,
   `created_at`) stays stable, so the request card and event
   cards continue to render against the same id across rounds.
4. Compose a thin "follow-up" preamble — "Agent X has a follow-up
   for you in the same line of work; your previous reply is above
   in this transcript. Their new request follows." — and call
   `SessionManager.start_message(child_sid, prompt)`. The
   broadcast subscriber that was already wired for this child id
   handles the next `result` event the same way it does a fresh
   delegation: `_inject_terminal` fires an
   `[agent-reply:<name> delegation=<id>]` into the parent, then
   auto-archives the child for the next round.

The model picks between `ask` and `follow_up` via the system-prompt
rule appended in `claude_code.py` / `codex.py` — keyed on whether
this is a *continuation* of an existing line of work with the same
agent. Cascade-cancel and the cycle/depth walk both still operate
on the persistent `delegation_id`, so the chain semantics survive
unchanged across rounds.

### 5.7 Working dir and `files` argument

`working_dir` for the child inherits from the parent by default —
"review the code" only makes sense in a directory context. The
optional `files=[…]` argument lets the parent name specific files;
they're resolved against the parent's `working_dir` and rendered into
the first user message as a path list. Missing paths are flagged as
`(not found)` rather than rejected; Vera reads files with her own
tools, just like a human would.

We do **not** automatically include any of the parent's transcript.
If Octo needs to give Vera context, it paraphrases.

### 5.8 Multiple concurrent sessions per agent

Confirmed in the inventory: `SessionManager` already supports
multiple live sessions per agent. We rely on this and explicitly do
**not** introduce a per-agent semaphore for v1.

Two consequences worth surfacing:

- **Shared memory dir, concurrent writes.** Vera's per-agent memory
  directory is shared across all her concurrent sessions. Native
  memory is per-file markdown with no transactional guarantees; two
  sessions writing the same fact file at the same instant can lose
  one write. We accept this as a known small race in v1 (it's the
  same race that already exists when Vera has, say, a user session
  open and a bridge session firing). If it becomes a problem we add
  a per-agent file lock around memory writes — but that's a
  `agent_memory.py` change, not a delegation-architecture change.
- **Shared credential, shared rate limit.** All of Vera's concurrent
  sessions hit the same upstream API key. Not our problem to police;
  we just don't multiply rate limits by N.

### 5.9 Nested delegation, cycle and depth

- **Depth cap: 3.** Root user → Octo → Vera → Pete is allowed. Pete
  cannot delegate further. The cap is enforced at creation time
  (walk the parent chain, count) and is a constant in code.
- **Cycle check.** During the walk, fail if any ancestor session
  belongs to the target agent. Octo → Vera → Octo is rejected.
  The walk also tracks visited session ids, so a corrupted pointer
  loop like A.parent=B / B.parent=A is rejected as a session-id cycle
  rather than mistaken for a valid short chain.
- **Fail-closed ancestor lookup.** The walk starts in memory and falls
  back to `db.load_sessions(include_archived=True)` when an ancestor
  is not live in `SessionManager`. That fallback is legitimate state:
  a delegation parent can be archived after its own terminal delivery,
  and an unarchived descendant must still be able to count that
  ancestor for depth and cycle checks. If neither memory nor DB has
  the ancestor row, reject; if the loop exhausts the safety cap, the
  `for/else` path rejects as pointer corruption (`server/delegations.py:399`,
  `server/delegations.py:470`, `server/delegations.py:488`).
- **Self-delegation rejected.** Octo cannot ask Octo. (If we ever
  want "give yourself a fresh scratch session", that's a separate
  feature with a different tool name.)

### 5.10 Memory and credential isolation — automatic

Both fall out of the existing per-agent wiring:

- Vera's child session has `agent_id=vera.id`, so
  `_make_run_config()` reads Vera's memory dir, credentials, system
  prompt, MCP set. No special-case code.
- Octo's memory dir is never touched by Vera's session, and vice
  versa. Memory writes during a delegation persist in *Vera's*
  memory — which is the right thing: she's the one learning.

## 6. Frontend rendering

The shipped UI renders delegation artifacts directly from the existing
chat stream plus delegation store state:

- **Delegation request card** — rendered in the **parent's**
  transcript next to the `mcp__ask_agent__ask` tool call. A compact
  card, shape similar to `ToolUseBlock`:

  ```
  ┌──────────────────────────────────────────────────┐
  │ 🐙 Octo → 🦁 Vera   • running   [delegation_id]  │
  │ "review the latest commit on dashboard branch"   │
  │                                                  │
  │ ▾ files: web/src/components/Dashboard.tsx        │
  │                                                  │
  │ [ Open Vera's session →  ]   [ Cancel ]          │
  └──────────────────────────────────────────────────┘
  ```

  It first matches the live `DelegationRunState` by parsing the
  sibling `tool_result` for `Started delegation \`<id>\`` and using
  that `delegation_id` as server truth. It only falls back to
  `(target_agent_name, request)` during the brief pre-result window,
  and deliberately avoids "by name only" matching because fan-out to
  the same agent would wire Cancel to the wrong child
  (`web/src/components/AgentDelegationRequestCard.tsx:71`). The card
  polls `GET /api/sessions/{sid}/delegations` until it sees the
  record terminate; no dedicated delegation-state WS event exists yet.

- **Delegation event card** — rendered in the parent's transcript
  whenever one of the three injected turns lands (`agent-reply`,
  `agent-question`, `agent-error`). Card with Vera's identity in the
  header (visually distinct from "Vera is the user" — she's speaking
  from *outside* into Octo's session).

  For `agent-question` cards specifically: the options are *not*
  shown as clickable buttons in Octo's UI (the human is not supposed
  to answer them — Octo is). They render as plain text inside the
  card. This is a deliberate UX choice that enforces the principal
  chain rule.

  The event card resolves the child session from `sessions ??
  archivedSessions`. If neither store contains the id, it fetches
  `/api/sessions/{delegation_id}` and inserts the response into the
  live or archived bucket based on the row's `archived` flag
  (`web/src/components/AgentDelegationEventCard.tsx:131`). The request
  card uses the same fetch-and-bucket logic before navigating, because
  terminal delegation children are usually archived by the time the
  user clicks through (`web/src/components/AgentDelegationRequestCard.tsx:142`).

- **Child session view.** Vera's session renders normally, with a
  header banner:

  ```
  Delegated from Octo (session a1b2c3)  •  Open parent →
  ```

  No other change. The chat below is identical to a normal session
  — the parent's request is the first user message.

- **Sidebar.** Delegation sessions are hidden from the default
  sessions list view (they'd otherwise flood it during heavy
  fan-out). Toggle: "Show delegations" in the filter dropdown. When
  hidden, an agent that has any open inbound delegations gets a
  small badge: "Vera • 2 delegations".

## 7. Implementation phases

Phased so each phase is independently shippable, testable, and gives
a usable demo at its end. All five phases have shipped. The review
rounds after the original plan added the important polish called out
above: short MCP tool names, cancel single-inject idempotency,
terminal-delivery auto-archive timing for nested chains,
cascade-cancel, archived-ancestor DB fallback in the chain walk, and
archived-child UI navigation.

**Phase 1 — Data model + delegation creation, no LLM yet.**

- Migration: `parent_session_id`, `delegation_request`, extend
  `origin` validator.
- API: `POST /sessions/{sid}/delegations`, `GET /sessions/{sid}/delegations`,
  `POST /sessions/{sid}/delegations/cancel`.
- `DelegationRunState` registry + broadcast listener.
- Hand-test by creating a child session directly via the API (no
  `mcp__ask_agent__ask` MCP server yet); confirm reply injection works
  end-to-end by faking `assistant_text` events.
  **Status: shipped.**

**Phase 2 — `ask_agent` MCP server (replies only).**

- `server/mcp_servers/ask_agent.py` with exported
  `ask` / `cancel` / `list` tools.
- Add to the default built-in set.
- Cycle and depth guards.
- Real end-to-end: a user tells Octo "ask Vera to summarise the
  README"; Vera summarises; Octo gets the reply and relays.
  **Status: shipped.**

**Phase 3 — Caller-aware `ask`, plus `mcp__ask_agent__answer`.**

- Caller-aware question routing via the delegation broadcast listener.
- `answer` tool on the `ask_agent` server.
- Real end-to-end: Vera asks a clarifying question ("which file?"),
  Octo answers from its own context, Vera finishes.
  **Status: shipped.**

**Phase 4 — Frontend.**

- Request/event cards in `MessageBubble.tsx` over existing
  `tool_use` / injected-message shapes.
- Child-session "Delegated from" header.
- Sidebar badge + filter toggle.
  **Status: shipped.**

**Phase 5 — Polish + nested delegation in anger.**

- Pete-under-Vera tests (3 hops).
- The "Octo asks the user when it doesn't know" loop is exercised in
  real-CLI/backend coverage.
- Memory write-race observation under heavy fan-out (no fix in v1
  unless we actually see corruption).
  **Status: shipped; memory write-lock remains deferred.**

## 8. Tests

Coverage landed across backend unit tests, real-CLI tests, frontend
component tests, and a browser e2e. The old "added counts" from the
pre-merge plan are intentionally not repeated here; the useful thing
now is the behavioral surface under test.

**Backend unit (pytest)** — new file `tests/test_delegations.py`:

Schema and routes are covered: `parent_session_id`,
`delegation_request`, `origin='delegation'`, target resolution
(case-insensitive, ambiguous → 409, missing → 404, self → 409), route
list/cancel/answer behavior, and `ON DELETE SET NULL` semantics.

Manager behavior is covered for reply/error/cancel injection,
question routing, `mcp__ask_agent__answer` draining the child's
pending question, empty/malformed question events, terminated
delegations ignoring late questions, concurrent same-target
delegations, bridge non-fan-out, and depth/cycle guards. The review
fixes have direct regression tests: terminal auto-archive happens from
`_inject_terminal`, the idle hook does not archive delegation parents
too early, nested Pete-under-Vera delivery survives Vera's idle
window, archived ancestors are found through the DB fallback,
session-id pointer cycles fail closed, cancellation cascade-cancels
descendants, and cancellation injects exactly one terminal turn even
when interrupt emits an error broadcast.

**Backend real-CLI** — new file `tests/test_delegations_real.py`,
auto-skips when CLIs not on PATH. Two agents, both running the
real harness:

- Octo (claude-code) `mcp__ask_agent__ask` to Vera (claude-code), 2-turn
  exchange. Vera's reply lands as a follow-up turn; Octo's next
  output references it.
- Octo (claude-code) `mcp__ask_agent__ask` to Vera (codex), same shape — proves
  the design is harness-agnostic.
- Vera asks one question via `ask` → injected to Octo → the same
  manager answer path used by `mcp__ask_agent__answer` drains the
  pending question → Vera completes.
- 3-hop chain: user → Octo → Vera → Pete. Pete replies, Vera relays,
  Octo summarises. Verifies depth-allowed-up-to-3 works.

**Frontend unit (vitest)**:

- `AgentDelegationRequestCard` renders running/completed states,
  matches by `delegation_id` parsed from the sibling tool result,
  avoids unsafe name-only matching, opens archived children by fetching
  `/api/sessions/{delegation_id}`, and POSTs cancellation to the
  scoped cancel route.
- `AgentDelegationEventCard` parses reply/question/error prefixes,
  renders question options as text, displays the real
  `mcp__ask_agent__answer` tool name, and can open children that live
  only in `archivedSessions`.

**E2E (Playwright)** — new spec `web/e2e/agent-collaboration.spec.ts`:

The browser spec runs against Playwright's auto-started backend with a
real `claude` binary. It covers `mcp__ask_agent__ask` spawning Vera's
child session, the inline request card rendering and transitioning to
`completed`, the injected `[agent-reply:…]` event card, click-through
into the child session, the "Delegated from Octo" header, return to
the parent, and the sidebar's hidden-delegation pill/toggle. The
question loop and 3-hop chain remain in backend real-CLI coverage,
where they are cheaper and less flaky.

## 9. Decisions baked in

- **A delegation is a Session row.** No new table, no parallel id
  space. The delegation id is the child session id.
- **Async only, bg-task-style.** No sync mode, no `wait_seconds`.
  Replies/questions/errors arrive via the existing turn-injection
  mechanism.
- **Caller-rule is universal.** The `ask` MCP server is made
  caller-aware; questions always travel one hop up. The parent
  decides whether to answer, escalate, or fail. This is enforced by
  the UI (no clickable options on `agent-question` cards in the
  parent's view) and by the absence of any cross-session jump.
- **No automatic transcript sharing parent → child.** Only `request`
  + `files=[…]` cross the boundary.
- **`working_dir` inherits from parent**, because directory context
  is almost always what makes the request actionable.
- **Multiple concurrent sessions per agent: unbounded.** Memory race
  acknowledged and accepted in v1.
- **Permissioning: open by default.** No allow/deny lists.
- **Depth cap: 3.** Self-delegation rejected. Cycles rejected.
- **Tool approval requests inside the child stay in the child.**
  Only `ask`-shaped questions bubble. Tool policy is the agent's
  decision, not the caller's.
- **Bridges don't fan out delegation sessions.** Telegram (and any
  future bridge) ignores `origin='delegation'`.
- **Cascade-cancel is part of v1.** Cancelling Vera also cancels
  Pete if Vera asked Pete and Pete is still running. Each hop still
  receives its own terminal turn; the cascade only prevents orphaned
  descendant work.
- **Tool names.** The MCP server is mounted as `ask_agent`; the
  exported tools are short: `mcp__ask_agent__ask`,
  `mcp__ask_agent__answer`, `mcp__ask_agent__cancel`,
  `mcp__ask_agent__list`. The longer names remain Python function
  names inside `server/mcp_servers/ask_agent.py` and are not the names
  the model should call.

## 10. What this defers, on purpose

- **Per-agent allow/deny lists** for who can be asked by whom. The
  schema is forward-compatible (an `agents.askable_by` text column
  later). Build when there's a real use case.
- **A "delegations" dashboard page.** The sidebar badge + filter
  toggle are enough for v1; a dedicated view can wait for evidence
  of need.
- **Question forwarding shortcut UI.** Today: if Octo's model decides
  it can't answer, it manually invokes its own `ask` tool and then
  feeds the human's answer back via `mcp__ask_agent__answer`. A
  future `forward_agent_question(delegation_id)` shortcut tool could
  collapse this into one call, but only if the model proves bad at
  the two-step.
- **Streaming replies.** Vera's reply is injected on `result`, as
  one chunk. Future: stream `assistant_text` deltas into the parent
  card so Octo's user can watch Vera type in real time. Worth doing
  but not a v1 requirement.
- **Memory write-lock under concurrent delegations.** Add when
  observed, not pre-emptively.
