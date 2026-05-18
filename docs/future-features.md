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

