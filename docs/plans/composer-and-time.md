# Three small affordances: message time, composer history, `/model`

> **Status:** shipped — hover a message for its time, ArrowUp for what you sent, `/model` to switch model for one session.

> **Implementation status: SHIPPED** (2026-09-26). `messages.created_at` +
> `_tag_persisted` + `MessageTime`; `lib/composerHistory.ts` +
> `lib/injectedTurns.ts` wired into the composer's key handling;
> `sessions.model` + `RuntimeProfile.models` + `ModelPickerDialog`.

Three requests that arrived together, and turn out to share one theme — the
transcript and the composer knew things they weren't telling you.

---

## 1. When did that happen (`MessageTime`)

**The case.** A turn that ran twenty minutes reads, afterwards, as an ordered
list with no clock in it. "Did that tool call take four minutes or forty" was
unanswerable, which is exactly the question a long-running task provokes.

**What was missing.** `messages` had no time column at all — every other table
has `created_at`; this one never did. So the feature is a column, not a
formatter.

- `messages.created_at TEXT`, stamped in `_persist_message` rather than in the
  DB layer, so the caller's own `MessageContent` carries the same value onto the
  WebSocket event. One decision, two destinations: the row a reload reads and
  the event a watching tab gets.
- `_tag_persisted(event, seq, msg)` replaced six copies of `event["seq"] = seq`.
  Both facts ("which row is this") come from the same write, so they are
  attached in the same place; the second field would otherwise have been six
  more lines to forget.
- **No backfill.** Nothing else recorded per-message time, so rows written
  before the column stay NULL and render *no* time. A plausible-looking
  invented timestamp is worse than a blank.

**In the UI**, one wrapper in `ChatView.renderMessage` rather than a change per
branch of `MessageBubble` — so tool calls, results, errors and notices all get
it, not just prose. Absolutely positioned in the gap above its row, because a
label that reflowed the transcript on hover would move the message away from the
cursor that was pointing at it.

On touch there is no hover, so `@media (hover: none)` shows the times dimmed
instead of hiding them. A timestamp is information, not an action: the reason
delete buttons are *not* revealed that way (mobile.md §5) does not apply.

## 2. ArrowUp, like a shell (`composerHistory`)

Fingers already know this one, so the semantics are copied rather than invented:
Up walks back through what you sent, Down walks forward, and coming back past
the newest restores the draft you were typing when you started. At the oldest
entry it stays put and still consumes the key — falling through would jump the
caret inside a recalled multi-line message.

Two guards make it safe to live on the same keys as the caret:

* **The slash menu owns the arrows when it is open** (it always did).
* **Only on the first line (Up) or last line (Down)**, so a recalled multi-line
  message is still navigable. `caretWantsHistory` is that rule, and it is one
  string test rather than a measurement of rendered lines.

The list is the user's own typed turns. A user-role message the user *never
typed* — a bg-task result, a delegation reply, a scheduled fire — is machine
text wearing a user's hat, and recalling one into the box would be nonsense.
That predicate already existed inside `ForkDialog`; it is now
`lib/injectedTurns.ts`, used by both, because two copies of a marker list drift
and the drift is invisible until a marker is added.

Kept pure (`lib/composerHistory.ts`) because the interesting part is the state
machine — in particular *when a key is not ours* — which is far easier to pin
directly than through a rendered textarea.

## 3. `/model` — per session, not per agent

**The semantics.** Wanting a stronger model for one hard question is not wanting
to re-point every session the agent owns. So `/model` writes
`sessions.model`, which wins over `agents.model`, whose own NULL still means
"let the CLI choose". Exactly the shape `sessions.backend` already had — the
precedent decided this, not taste.

**Not an allow-list.** `PATCH /api/sessions/{id}` stores whatever string it is
given. Both CLIs accept names this build cannot know, and a model released next
week must not need an Octopus release to be usable. What the picker offers is a
*shortlist*, from three sources, each answering a different question:

| Source | Why |
|---|---|
| The agent's default | The way back. Choosing it clears the override, so the session keeps following the agent if the agent is later re-pointed. |
| `RuntimeProfile.models` per backend | The names that harness's CLI is known to take. Claude's are the documented *aliases* (`opus`/`sonnet`/`haiku`) rather than pinned ids, so the list doesn't go stale every release. **Codex's is deliberately empty**: nothing in this repo establishes which names `codex -m` accepts, and a guessed shortlist that silently fails is worse than none. |
| Models already in use by this user's agents and sessions | Real data. A name this build never heard of appears once it has been used — which is also what keeps Codex from being an empty menu. |

`/model <name>` applies straight away; bare `/model` opens the picker. Both
answer with a notice saying it takes effect from the next turn, because it does.

---

## What this defers

**Showing the model in force in the chat header.** The composer says what
changed when it changes, and `SessionInfo.model` is now on the wire, so the
header could say it permanently. Whether it earns the space next to the
breadcrumb is a design call about a crowded bar, not a plumbing question.

**A models endpoint that asks the CLIs.** Both could, in principle, be
interrogated for their model lists. Neither documents a stable way to do it, and
a probe that shells out on every picker open to answer a question the free-text
field already answers is not worth it until one of them ships the flag.
