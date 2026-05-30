# Agent Collaboration — Tech Plan (agent-to-agent delegation)

> **Implementation drift note (post-merge).** While building this we
> renamed the underlying Python helpers (`ask_agent`,
> `answer_agent_question`, `cancel_agent_task`, `list_agent_tasks`) to
> the shorter MCP names the model actually sees: **`ask`**,
> **`answer`**, **`cancel`**, **`list`** — full forms
> `mcp__ask_agent__ask` / `…__answer` / `…__cancel` / `…__list`.
> Wherever the prose below uses the longer names, treat them as
> referring to those four MCP tools. Likewise, `mcp__bg__run`-style
> references in this doc match the actual surface.
>
> Also: §5.2 below says delegation children "auto-archive on idle".
> The implementation refines that: a delegation child is archived
> **once its own terminal `[agent-…]` turn has been injected into the
> parent**, not on every idle transition. That distinction is
> load-bearing for nested chains — an intermediate parent (Vera in
> Octo→Vera→Pete) is idle while waiting for its grandchild's reply
> and must NOT vanish in that window.

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
- A new `ask_agent` MCP tool is the inverse: "become a caller — spawn
  a child session under another agent and wait for it to talk back".
- A parent agent that receives a delegated question decides, in plain
  language, whether to answer it directly, ask its *own* caller
  (recursing one hop up), or fail the delegation. No special-case
  cross-session UX; it's just the model's normal tool-use loop.

We piggy-back on Octopus's existing **bg-task pattern** for the
async plumbing. `ask_agent` returns a delegation id immediately, the
agent's turn ends, and when the child session produces a reply (or
question, or terminal error) Octopus injects a follow-up turn into the
parent session. No new long-poll infrastructure, no blocking
subprocesses, parallel fan-out for free.

This plan reverses the explicit "no A2A" carve-out from
`agent-refactor.md` §40-41 / §304. That carve-out conditioned A2A on
a Run table or a "trigger mechanism". Neither turned out to be
needed: a delegation is a normal `Session` row with a
`parent_session_id`, and the trigger is just an MCP tool call.

## 1. Goals

- A built-in MCP tool `ask_agent(name, request, files?)` available to
  every agent by default, by which an agent can delegate a request to
  another named agent.
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
  delegations inline as a new message type; that's it. The user can
  always click through into the child session for the full
  transcript.

## 3. Reference: the bg-task pattern as template

`server/mcp_servers/bg.py` is the closest existing thing and we want
to mimic its shape almost exactly:

| bg-task | agent-delegation |
|---|---|
| `bg_run(command, description?)` | `ask_agent(name, request, files?)` |
| Returns `task_id` immediately | Returns `delegation_id` immediately |
| Parent turn ends | Parent turn ends |
| Subprocess runs in background | Child Session runs in background |
| Captured stdout/stderr | Captured `assistant_text` events |
| On completion: inject follow-up turn `[bg-task-result …]` | On completion: inject follow-up turn `[agent-reply:<name> …]` |
| `bg_cancel(task_id)` SIGTERM | `cancel_agent_task(delegation_id)` stops the child session |
| `bg_list()` recent tasks | `list_agent_tasks()` recent delegations |

The follow-up-turn injection mechanism (`server/session_manager.py`
'inject system turn' path used by bg) is the load-bearing piece we
reuse for **three** kinds of inbound events from the child:

- `[agent-reply:<name> delegation=<id>] <text>` — terminal success
- `[agent-question:<name> delegation=<id> options=…] <text>` — child
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
parent. The parent's `answer_agent_question` tool call posts the
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

### 5.1 `ask_agent(name, request, files?)` — invocation

A new built-in MCP stdio server, `server/mcp_servers/ask_agent.py`,
shaped exactly like `bg.py`. Three tools: `ask_agent` (start),
`cancel_agent_task` (stop), `list_agent_tasks` (introspect). Env
injection (`OCTOPUS_API_BASE`, `OCTOPUS_AUTH_TOKEN`,
`OCTOPUS_SESSION_ID`) matches the existing pattern; the server is a
thin HTTP shim to FastAPI routes.

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
   `SessionManager.start_message(...)` path. Subscribe an internal
   delegation listener (see 5.3) to the child's event stream.
6. **Return immediately** `{delegation_id: child_session.id, status:
   "started"}`. The MCP shim translates this to a short text the
   model can quote: "Started delegation `<id>` to Vera. I'll receive
   her reply as a follow-up turn."

### 5.2 Sub-session lifecycle

A delegation child session is an ordinary session for every purpose
except two:

- **Auto-archive on idle.** When the child's session goes idle after
  the terminal event (reply / error / cancellation) it is archived
  the same way `origin='schedule'` sessions are (per `agent-refactor.md`
  §5.6). It is not auto-deleted — the user can still browse it from
  Vera's archived-sessions list.
- **No bridge fan-out.** Bridges only broadcast to `origin='user'` (and
  maybe `bridge`) sessions; a delegation must not also notify the
  user's Telegram chat. Single line in `BridgeManager._on_broadcast`.

Otherwise: same harness, same MCP set, same credentials, same memory
dir as Vera's other sessions. Memory writes from concurrent
delegations to the same agent share the agent's memory dir — see 5.8
on concurrency.

### 5.3 Reply delivery: the delegation listener

For each live delegation, the FastAPI process holds a small in-memory
record `DelegationRunState` keyed by child session id:

- `parent_session_id`
- `target_agent_name` (cached so we can format the injection prefix)
- `captured_text: list[str]`
- `state: "running" | "completed" | "failed" | "cancelled"`
- `task: asyncio.Task` — the subscription coroutine

The subscription coroutine drinks the child's `SessionManager` event
stream with the **same filter as bridge quiet-mode** (per the
inventory: `assistant_text` + `error` + `tool_approval_request`, drop
`tool_use`/`tool_result`/`result`/`status`). Behavior per event kind:

- `assistant_text` → append chunk to `captured_text`.
- `result` (terminal) → finalise. Build the injection string:

  ```
  [agent-reply:vera delegation=ab12cd34ef56]
  <joined captured_text>
  ```

  Call `SessionManager.inject_system_turn(parent_session_id, text)`
  (the same hook bg uses). Mark `state="completed"`, drop the record
  from the registry after a short retention window (so
  `list_agent_tasks` can still see recent ones).
- `error` → finalise with `[agent-error:…]`, same injection path.
- `tool_approval_request` → if it's the `ask` MCP server requesting
  an answer, this is a delegated question; see 5.4. Other tool
  approval requests (e.g. dangerous shell command) **stay in the
  child session** — they're the *agent's* policy, not the user's.
  This means the delegation can stall waiting on Vera's own approval
  policy; we surface that via a UI dot but do not bubble it. (If
  Vera's tool policy needs a yes/no the user has to open Vera's
  session and decide. Bubbling tool approvals would obliterate the
  "questions go through the model" rule.)

### 5.4 Question delivery: caller-aware `ask`

Today the `ask` MCP server's flow is roughly:

```
ask.tool(question, options) -- HTTP --> POST /sessions/{sid}/questions
                                         pending_questions.put(...)
                                         broadcast to UI websocket
                                         wait for answer
                                         return answer to model
```

We change one thing in the FastAPI handler: **after** putting the
question on `pending_questions` and broadcasting to the websocket,
check `session.parent_session_id`:

```python
if session.parent_session_id is not None:
    parent_agent = ...
    injection = (
        f"[agent-question:{this_agent.name} "
        f"delegation={session.id} options={json.dumps(option_labels)}]\n"
        f"{question}"
    )
    SessionManager.inject_system_turn(session.parent_session_id, injection)
```

The pending question already has a unique id; nothing else changes.
The parent's model sees the injected turn and is expected to call
`answer_agent_question(delegation_id, choice)`.

### 5.5 `answer_agent_question(delegation_id, choice)` — closing the loop

A new MCP tool on the `ask_agent` server. Body posts to
`POST /api/sessions/{child_sid}/questions/answer` with the chosen
option label (or freeform text if the question allowed it).

Server-side: identical to the existing path that fires when a human
clicks an option in the UI — it drains the same pending entry and
returns the answer to the child's `ask` MCP call. The model is the
parent agent; the websocket UI is unaware. Both producers (human
click, parent tool call) compete to drain the same queue; first one
wins. If both happen, the second one 409s.

If the parent's model decides it can't answer, it has three options
expressible in its normal language/tool loop:

- Call its **own** `ask` tool to ask its caller (recursing one hop
  up). When the answer comes back, call `answer_agent_question` with
  it. This is the "I don't know — let me ask the user" path.
- Call `cancel_agent_task(delegation_id, reason="…")`. The child gets
  an exception in its `ask` call and finalises via the error path.
- Do nothing. The child stays waiting indefinitely. The user can
  open the child session and answer in the UI as the manual fallback.

### 5.6 `cancel_agent_task` and `list_agent_tasks`

`cancel_agent_task(delegation_id, reason?)` →
`POST /api/sessions/{child_sid}/delegations/cancel`. Server stops the
child's running turn (the existing session-stop path), if a pending
question is open it gets a "cancelled" answer, the
`DelegationRunState` is finalised with `state="cancelled"` and the
parent gets an injected `[agent-error:vera delegation=… reason=cancelled]`.

`list_agent_tasks()` → `GET /api/sessions/{parent_sid}/delegations`,
returns the live + recently-finished `DelegationRunState` records for
this parent, capped at 25, most recent first. Symmetric with
`bg_list`.

### 5.7 Working dir and `files` argument

`working_dir` for the child inherits from the parent by default —
"review the code" only makes sense in a directory context. The
optional `files=[…]` argument lets the parent name specific files;
they're resolved against the parent's `working_dir`, validated to
exist, and rendered into the first user message as a path list. We
do not stream file contents — Vera reads them with her own tools,
just like a human would.

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

Two new message types in the chat stream (`web/src/types.ts` and
`MessageBubble.tsx`):

- `agent_delegation_request` — rendered in the **parent's** transcript
  at the point of the `ask_agent` tool call. A collapsible card,
  shape similar to `ToolUseBlock`:

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

  Updates to `replied`, `cancelled`, `failed`, `awaiting answer`
  in-place by listening to the same WS event stream the chat uses
  today.

- `agent_delegation_event` — rendered in the parent's transcript
  whenever one of the three injected turns lands (`agent-reply`,
  `agent-question`, `agent-error`). Card with Vera's avatar on the
  left edge (visually distinct from "Vera is the user" — she's
  speaking from *outside* into Octo's session).

  For `agent-question` cards specifically: the options are *not*
  shown as clickable buttons in Octo's UI (the human is not supposed
  to answer them — Octo is). They render as plain text inside the
  card. This is a deliberate UX choice that enforces the principal
  chain rule.

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
a usable demo at its end.

**Phase 1 — Data model + delegation creation, no LLM yet.**

- Migration: `parent_session_id`, `delegation_request`, extend
  `origin` validator.
- API: `POST /sessions/{sid}/delegations`, `GET /sessions/{sid}/delegations`,
  `POST /sessions/{sid}/delegations/cancel`.
- `DelegationRunState` registry + subscription coroutine.
- Hand-test by creating a child session directly via the API (no
  `ask_agent` MCP server yet); confirm reply injection works
  end-to-end by faking `assistant_text` events.

**Phase 2 — `ask_agent` MCP server (replies only).**

- `server/mcp_servers/ask_agent.py` with `ask_agent` /
  `cancel_agent_task` / `list_agent_tasks` tools.
- Add to the default built-in set.
- Cycle and depth guards.
- Real end-to-end: a user tells Octo "ask Vera to summarise the
  README"; Vera summarises; Octo gets the reply and relays.

**Phase 3 — Caller-aware `ask`, plus `answer_agent_question`.**

- One-line change in the `ask` FastAPI handler to also inject into
  the parent session when one exists.
- `answer_agent_question` tool on the `ask_agent` server.
- Real end-to-end: Vera asks a clarifying question ("which file?"),
  Octo answers from its own context, Vera finishes.

**Phase 4 — Frontend.**

- New message types + cards in `MessageBubble.tsx`.
- Child-session "Delegated from" header.
- Sidebar badge + filter toggle.

**Phase 5 — Polish + nested delegation in anger.**

- Pete-under-Vera tests (3 hops).
- The "Octo asks the user when it doesn't know" loop is exercised in
  e2e.
- Memory write-race observation under heavy fan-out (no fix in v1
  unless we actually see corruption).

## 8. Tests

Mirror the existing test taxonomy in `CLAUDE.md`. All counts here are
*added* tests; the existing 685 backend / 46 frontend / 61 e2e
numbers should grow accordingly.

**Backend unit (pytest)** — new file `tests/test_delegations.py`:

- Schema: `parent_session_id` column exists, FK behaviour
  (SET NULL on parent delete), `origin='delegation'` accepted.
- `POST /sessions/{sid}/delegations` resolves target by name
  (case-insensitive, ambiguous → 409, missing → 404, self → 409).
- Cycle guard: Octo → Vera → Octo rejected.
- Depth guard: 4-hop chain rejected at creation.
- Reply injection: fake `assistant_text` + `result` events on a child
  session land as a properly-formatted `[agent-reply:…]` turn on the
  parent (via the registry's subscription coroutine).
- Error/cancel injection: same, with the appropriate prefix.
- Caller-aware `ask`: a child session calling `ask` with no human
  attached produces an `[agent-question:…]` injection on the parent
  AND a pending question on the child; `answer_agent_question` from
  the parent drains it and returns the answer to the child's tool
  call.
- Concurrency: two delegations to the same target agent run
  simultaneously without deadlock; both reply.
- Bridge fan-out: `origin='delegation'` does NOT trigger the
  Telegram bridge.

**Backend real-CLI** — new file `tests/test_delegations_real.py`,
auto-skips when CLIs not on PATH. Two agents, both running the
real harness:

- Octo (claude-code) `ask_agent` to Vera (claude-code), 2-turn
  exchange. Vera's reply lands as a follow-up turn; Octo's next
  output references it.
- Octo (claude-code) `ask_agent` to Vera (codex), same shape — proves
  the design is harness-agnostic.
- Vera asks one question via `ask` → injected to Octo → Octo answers
  via `answer_agent_question` → Vera completes.
- 3-hop chain: user → Octo → Vera → Pete. Pete replies, Vera relays,
  Octo summarises. Verifies depth-allowed-up-to-3 works.

**Frontend unit (vitest)**:

- `MessageBubble` renders both new message types.
- Delegation card status transitions on WS events.
- `agent-question` cards render options as text, not buttons.

**E2E (Playwright)** — new spec `web/e2e/agent-collaboration.spec.ts`:

- Two-agent fixture (Octo + Vera) with the fake harness used by the
  existing e2e suite.
- Golden path: user says "ask Vera to write a poem"; Vera's reply
  card appears in Octo's chat within a few seconds; user can click
  through to Vera's session.
- Question loop: Vera asks "what topic?"; an `agent-question` card
  appears in Octo's chat; Octo (driven by the fake harness)
  answers; Vera completes.
- Cancel: user-triggered cancel button on the delegation card
  produces an error card in Octo's chat.
- Sidebar filter: delegations hidden by default, toggle shows them.

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
- **Tool names**: `ask_agent`, `answer_agent_question`,
  `cancel_agent_task`, `list_agent_tasks` — `ask_agent` chosen to
  mirror the existing `ask` (ask the caller) vs `ask_agent` (ask
  another agent).

## 10. What this defers, on purpose

- **Per-agent allow/deny lists** for who can be asked by whom. The
  schema is forward-compatible (an `agents.askable_by` text column
  later). Build when there's a real use case.
- **A "delegations" dashboard page.** The sidebar badge + filter
  toggle are enough for v1; a dedicated view can wait for evidence
  of need.
- **Question forwarding shortcut UI.** Today: if Octo's model decides
  it can't answer, it manually invokes its own `ask` tool and then
  feeds the human's answer back via `answer_agent_question`. A
  future `forward_agent_question(delegation_id)` shortcut tool could
  collapse this into one call, but only if the model proves bad at
  the two-step.
- **Streaming replies.** Vera's reply is injected on `result`, as
  one chunk. Future: stream `assistant_text` deltas into the parent
  card so Octo's user can watch Vera type in real time. Worth doing
  but not a v1 requirement.
- **Memory write-lock under concurrent delegations.** Add when
  observed, not pre-emptively.
