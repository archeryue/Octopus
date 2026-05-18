# Future Features

What's still open. Items that landed are removed; the "done" record
lives in `STEAL_PLAN.md` and the git log.

---

## 1. CodexBackend (the second backend)

**Priority**: High
**Affected**: new `server/backends/codex.py`, frontend backend selector
in the session-create form

### Status

The `BackendBase` / `SubprocessJsonlBackend` abstraction is in place
and `ClaudeCodeBackend` already runs on it. The Codex slot exists in
`BackendKind` (`models.py`) but the concrete class isn't shipped yet.

### What's left

- `server/backends/codex.py`: subclass of `SubprocessJsonlBackend`,
  build_args returns `codex exec --json ...` or `codex resume <id>
  --json`, `_normalize` maps Codex's `thread.*` + `item.*` events to
  `BackendEvent`.
- `_make_backend` in `session_manager.py` dispatches on
  `session.backend` instead of always returning `ClaudeCodeBackend`.
- Session-create form: backend selector (radio), shown if Codex is
  installed (check `which codex` server-side and surface a flag).
- Per-backend credential routing already works via the existing
  `credential_id` plumbing.

### Backend differences to keep in mind

| Aspect | Claude Code | Codex |
|---|---|---|
| Binary | `claude` (npm `@anthropic-ai/claude-code`) | `codex` (npm `@openai/codex`) |
| Session unit | Streaming stdin/stdout JSONL | One-shot exec per turn |
| Event vocabulary | `user/assistant/system/result` + control protocol | `thread.started`, `turn.*`, `item.*` |
| Per-tool callback | Yes (control protocol) | No — sandbox-level only |
| Auth | `~/.claude/auth.json` or `ANTHROPIC_API_KEY` | `codex login` or `OPENAI_API_KEY` |
| Built-in `AskUserQuestion` | Yes | No (no native equivalent) |

The Frontend should hide the per-tool approval UI when the active
session is Codex, since Codex enforces approval at the sandbox level
instead.

---

## 2. Email integration (Bridge)

**Priority**: Medium
**Affected**: new `server/bridges/email.py`, `server/config.py`

Generalize the existing `server/bridges/` pattern (Telegram already
ships) to email. Both directions:

- **Inbound**: IMAP poll or Gmail-API push → route an incoming email
  to a chosen session as a user message (same path the scheduler /
  Telegram use).
- **Outbound**: SMTP or Gmail API for sending; Claude can draft
  replies the user reviews before send.

Use cases: forward incoming mail to a session for triage, scheduled
email digests (composes with the schedules feature), draft-and-review
replies.

---

## 3. In-app markdown / file reader

**Priority**: Medium
**Affected**: new `web/src/components/FileViewer.tsx`, new
`GET /api/sessions/{id}/files?path=...` endpoint

Claude often writes / edits markdown in the session's `working_dir`
(READMEs, plans, notes). Today the user has to switch to a separate
editor to read them. Add a side-panel viewer that opens files
directly from the chat (`View` button next to `Write` / `Edit` /
`MultiEdit` tool calls).

### Security model
- Resolve the requested path against the session's `working_dir`
  using `realpath` + `commonpath`; reject anything outside.
- Refuse symlinks that escape the dir.
- Size cap (1 MiB-ish) returns 413 with a hint.
- Refuse binaries (extension allowlist + null-byte sniff).
- Standard `Authorization: Bearer <token>` auth.

### Scope (MVP)
- Open the file Claude just touched (no file browser).
- Markdown rendered via the same `react-markdown` config the chat
  uses; everything else in a `<pre>` block.
- Reload button to re-fetch.

### Scope cuts (deferred)
- Directory tree, inline editing, diff view, image preview, live
  watching via SSE, syntax highlighting.

---

## 4. AskUserQuestion: replace deny-as-answer with a custom MCP tool

**Priority**: Low — code-smell cleanup, not a user-visible bug
**Affected**: `server/backends/claude_code.py`, new MCP tool definition

Today's flow: when the CLI asks "can_use_tool AskUserQuestion?", we
emit a `question_request` event, hold the control_request, and when
the user answers we respond with `behavior=deny, message=<answer>`.
The CLI surfaces the deny `message` to Claude verbatim — Claude reads
it as the tool's effective response and continues. The real-CLI e2e
test (`new-features.spec.ts::AskUserQuestion: real model → form …`)
passes, so this works.

The semantic awkwardness: we're using the "denied with reason" path
for what's really "answered with content". A cleaner alternative
exists but is more work: register our own MCP server that exposes
`AskUserQuestion`, disable the CLI's built-in via
`disallowed_tools=["AskUserQuestion"]`, and return the answer as a
normal tool_result. This avoids the protocol-shape mismatch but
introduces an MCP server we have to lifecycle.

Verdict: leave the current shape until we have another reason to add
MCP plumbing (e.g. exposing more host-side tools). Keep the existing
behavior covered by the e2e tripwire that fails on any `ZodError` or
`Tool permission request failed` text.

---

## 5. Async notification target (push when UI is closed)

**Priority**: Medium
**Affected**: new `server/notifiers/`, schedule-failure / session-done
hooks in `session_manager.py`

Today the only async ping path is the Telegram bridge — and it's
bound to whatever chat is configured for it. If you close the tab,
nothing wakes you when a session finishes a long turn, a schedule
fires, or an `AskUserQuestion` is pending.

### Direction

Generalise the bridge pattern into a "notifier" target. Each notifier
knows how to send one short message somewhere; triggers fire on
session-status transitions.

- Notifiers: webhook (POST JSON), browser push (Web Push API), email,
  ntfy.sh, Slack. Telegram already qualifies and folds in.
- Triggers: session went idle, AskUserQuestion pending > N seconds,
  schedule failed, tool needs approval > N seconds.
- Config: per-target enable/disable, optional per-session opt-in.

### Scope (MVP)
- One notifier type (webhook) + one trigger (session went idle).
- Sidebar settings UI to manage targets.
- Rest comes in follow-ups.

---

## 6. Per-session WebSocket reconnect — drop the setMessages race

**Priority**: Low (narrow window, not user-reported)
**Affected**: `web/src/hooks/useWebSocket.ts`, `server/session_manager.py`,
`server/database.py`, `server/routers/sessions.py`

On WS reconnect, `onopen` fetches `/api/sessions/{activeId}` and calls
`setMessages` with the result, which replaces the in-memory array. If
the server broadcasts a fresh event between the HTTP response
arriving and `setMessages` running, that event lands via `onmessage`
and then gets stomped by `setMessages`.

The persistence order (persist → broadcast) means the data is never
lost on the server (the next reconnect's refetch would catch it),
but the live UI can briefly miss the most-recent event until the
next event triggers a re-render.

**Why this needs more than a one-file fix**: naive buffer-and-replay
double-applies any event that's already in the snapshot. Safe dedup
needs a monotonic key on each WS event that's stable across
snapshot + broadcast. The natural choice is the `messages.seq`
column the DB already maintains.

**Plan**:
1. `database.load_messages` returns `seq` per row.
2. `_persist_message` captures the assigned seq.
3. `session_manager` includes `seq` on every WS event whose payload
   corresponds to a DB row (`user_message`, `assistant_text`,
   `tool_use`, `tool_result`, `result`, `error`, `question_*`).
4. `SessionDetail` response includes the snapshot's high-water-mark
   seq.
5. Frontend tracks `lastAppliedSeq` per session in the store, drops
   any incoming event with `seq <= lastAppliedSeq`, and bumps the
   counter on every successful apply.
6. On `setMessages`, set `lastAppliedSeq` to the max seq in the
   snapshot.

Tracking under "low priority" because the window is small and
nothing is actually lost from the DB.

---

## 7. Settings dialog (additive)

**Priority**: Low

A "Settings" entry in the sidebar (gear icon, separate from the
three content sections) opens a Radix Dialog with internal tab nav:

- General: server URL, theme toggle (dark mode could come back as an
  opt-in), version.
- Account: display token, "copy", logout.
- Notifications: once the notifier framework lands.

This is the additive version of the original B-7 from STEAL_PLAN.md
(that one tried to relocate the sidebar sections, which we kept).
