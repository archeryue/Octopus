# Claude Code CLI JSONL Protocol Notes

Empirical capture of the `claude` CLI's stream-json protocol, used as the source of truth for `ClaudeCodeBackend` in `server/backends/claude_code.py` (feature 1 in `future-features.md`).

**Captured against**: `claude` v2.1.143, model `claude-haiku-4-5-20251001`.

**Reproduce**: `tests/_fixtures/cli-jsonl/` will hold the captured traces; the test suite replays them through the normalizer.

## Invocation

```
claude --print \
       --input-format=stream-json \
       --output-format=stream-json \
       --verbose \
       --permission-mode default \
       --permission-prompt-tool stdio \
       [--resume <session-id>] \
       [--include-partial-messages] \
       [--include-hook-events] \
       [--no-session-persistence]
```

Notes:
- `--verbose` is **required** with `--output-format=stream-json` under `--print` (otherwise CLI errors).
- `--permission-prompt-tool stdio` is a **hidden flag** (doesn't show in `--help` but is accepted). Tells the CLI to use the SDK control protocol over stdio for permission decisions.
- `--no-session-persistence` skips the disk write to `~/.claude/sessions/`. Without it, session is resumable via `--resume <session-id>`.

## Wire format

- One JSON object per line on **stdout**.
- All events share a `type` field; many also carry `session_id` and `uuid`.
- **stderr** is used for warnings and fatal errors (not used for events in normal operation).

## Event types observed

| Type | Subtype | Meaning |
|---|---|---|
| `system` | `init` | First event: full server config — `session_id`, `tools[]`, `model`, `permissionMode`, `cwd`, `slash_commands[]`, `agents[]`, `apiKeySource`, `claude_code_version`, `memory_paths`, etc. |
| `system` | `status` | Informational status pings (`requesting`, `responding`, …). Safe to ignore for chat UX. |
| `rate_limit_event` | — | Rate-limit status snapshot. Informational. |
| `assistant` | — | Model message. `message.content[]` carries content blocks — see below. |
| `user` | — | Tool results echoed back by the CLI's built-in tool runner (Bash, Edit, etc.). `message.content[]` carries `tool_result` blocks. Also: any prompt the host streams in over stdin shows up here. |
| `result` | `success` or `error_during_execution` | Terminal event for one turn. Includes `total_cost_usd`, `duration_ms`, `num_turns`, `session_id`, `usage`, `permission_denials[]`, `terminal_reason`. |
| `stream_event` | — | Only when `--include-partial-messages` is set. Wraps an Anthropic API stream chunk (`message_start`, `content_block_delta`, etc.). Useful for streaming partial text but optional. |

## Content blocks (inside `assistant.message.content[]` and `user.message.content[]`)

| Block `type` | Fields | Notes |
|---|---|---|
| `text` | `text` | Plain assistant text. |
| `thinking` | `thinking`, `signature` | Hidden reasoning. Don't render to user; persist if we want to show "thinking" toggles. |
| `tool_use` | `id`, `name`, `input`, `caller` | Model invoking a tool. `caller.type` is `direct` for top-level calls. |
| `tool_result` | `tool_use_id`, `content`, `is_error` | Result echoed by the CLI's tool runner. Sits inside a `user` event. |

**Important quirk:** for multi-block assistant messages, the CLI emits **one `assistant` event per content block**, all sharing the same `message.id` and `request_id`. So a thinking + text + tool_use turn → 3 separate stdout lines. The normalizer should treat them as independent events, not try to assemble them.

## Tool flow

```
assistant {content: [{type: "tool_use", id: X, name: "Bash", input: {...}}]}
user      {content: [{type: "tool_result", tool_use_id: X, content: "...", is_error: false}]}
assistant {content: [{type: "text", text: "..."}]}
result    ...
```

The CLI runs built-in tools (Bash, Read, Edit, Write, Glob, Grep, …) itself — no host involvement. We only see the `tool_use` and `tool_result` events.

## AskUserQuestion in headless mode — confirmed behavior

When the model calls `AskUserQuestion`:

1. CLI emits `assistant.tool_use` with `name: "AskUserQuestion"` and `input.questions[]`.
2. CLI internally needs an answer. With `--permission-prompt-tool stdio` and stdin already closed (`echo … | claude --print`), the CLI errors:
   - `user.tool_result` with `is_error: true`, `content: "Tool permission request failed: Error: Stream closed"`
3. Model sees the error, falls back gracefully (typically: emits text asking the question directly).
4. `result` event closes the turn normally.

**No hang.** The CLI fails fast when there's no live host to answer.

This answers the open question from the AskUserQuestion design: the CLI is in **case 2** from `future-features.md` #7 — it uses the control protocol over stdio to ask the host for an answer. If we keep stdin open and respond to the control_request, we can deliver a *real* tool_result. That's the clean fix for feature 7.

(We didn't capture the `control_request` event itself in these experiments because we pre-closed stdin. Capturing it requires keeping stdin open and responding interactively — to be done as part of Phase 1d.)

## Resume

`--resume <session-id>` works after the previous subprocess has fully exited (state persists in `~/.claude/sessions/`). The resumed run emits a new `system.init` event but keeps the same `session_id`. Confirmed: the new turn has full context from the prior conversation.

`--continue` resumes the most recent conversation for the current directory. We won't use it — too cwd-coupled.

## Interrupts (not empirically captured in this round)

The Python SDK's `interrupt()` sends a control_request over stdin. We need to capture this in Phase 1d when we stand up the bidirectional stdio handler. For now we assume the same control protocol works.

## What this means for `ClaudeCodeBackend`

1. Spawn `claude` with the flags above.
2. Keep stdin OPEN for the lifetime of the session — write user prompts and control_responses on demand.
3. Read stdout line-by-line, parse each as JSON, route by `type`.
4. Per-block emission: forward each `assistant.content[i]` block as its own normalized `BackendEvent` (text / tool_use / thinking).
5. For `user.tool_result`, forward as `BackendEvent(type="tool_result")`.
6. For `result`, capture `session_id` (for resume) + `total_cost_usd` + `duration_ms`; close the per-turn iterator.
7. For control_request events (Phase 1d): handle `can_use_tool` (auto-allow most, intercept `AskUserQuestion` properly).
8. Graceful shutdown: close stdin → wait for `result` → terminate; force-kill after timeout.

## Open questions for Phase 1d

- What does the `control_request` JSON shape look like on the wire? (SDK source suggests `{type: "control_request", request_id, request: {subtype: "can_use_tool", tool_name, input, ...}}`, but capture it live to be sure.)
- How is interrupt sent? Format of `control_request` with `subtype: "interrupt"`?
- Does `--resume` accept a session ID that doesn't yet exist on disk (e.g., when we want to specify our own)? `--session-id <uuid>` flag exists for this — confirm semantics.

---

## OAuth / login surface (CLI v2.1.143)

Reverse-engineered from the bundled `cli.js`, not run against the live CLI (the host I'm researching from won't let me spawn `claude` for permission reasons).

### Endpoints + constants (hardcoded in CLI)

```
CLAUDE_AI_AUTHORIZE_URL:   https://claude.ai/oauth/authorize
CONSOLE_AUTHORIZE_URL:     https://console.anthropic.com/oauth/authorize
TOKEN_URL:                 https://console.anthropic.com/v1/oauth/token
MANUAL_REDIRECT_URL:       https://console.anthropic.com/oauth/code/callback
CLAUDEAI_SUCCESS_URL:      https://console.anthropic.com/oauth/code/success?app=claude-code
CONSOLE_SUCCESS_URL:       https://console.anthropic.com/buy_credits?returnUrl=...
API_KEY_URL:               https://api.anthropic.com/api/oauth/claude_cli/create_api_key
ROLES_URL:                 https://api.anthropic.com/api/oauth/claude_cli/roles
CLIENT_ID:                 9d1c250a-e61b-4...  (truncated in dump)
SCOPES:                    [..., "user:profile", ...]
```

### Flow shape (PKCE + manual code paste)

The CLI uses an Ink (React-for-terminal) UI driven by a state machine:

```
idle → ready_to_start → waiting_for_login(url) → creating_api_key → success | error
```

Key functions in the bundled JS:
- `startOAuthFlow(onUrl, opts)` — opens the authorize URL in a browser, returns a promise that resolves with `{accessToken, scopes, ...}` after the user pastes the code back.
- `opts = {loginWithClaudeAi, inferenceOnly, expiresIn, orgUUID}`
- For `claude setup-token`: `inferenceOnly=true, expiresIn=31536000` (1 year). The token is shown to the user; nothing else stored.
- For regular login (`/login` slash command): token is stored, role + permission checks happen, success.

### Entry points

| How user starts login | Triggers |
|---|---|
| `claude` (no auth present) | TUI prompts; user types `/login` |
| `/login` slash command in REPL | Starts OAuth flow inline |
| `claude setup-token` | Headless-ish: runs the OAuth flow to issue a long-lived (1-year) token, prints the token, exits. Requires Claude subscription. |
| `claude auth status` | Read-only — confirmed working (returns JSON) |

### What the manual code paste looks like

After OAuth completes in the browser, the redirect lands the user at `MANUAL_REDIRECT_URL` (`console.anthropic.com/oauth/code/callback`) which displays an auth code. The user copies it back to the CLI input prompt. The CLI then POSTs the code to `TOKEN_URL` and gets back an access token.

### Implications for Octopus

Most pragmatic path for in-app login:
1. Spawn `claude setup-token` in a PTY (Ink TUI needs a TTY) with `HOME` pointed at a fresh per-credential dir.
2. Read stdout, regex-extract the authorize URL (matches `https://claude.ai/oauth/authorize?...` or `https://console.anthropic.com/oauth/authorize?...`).
3. Surface URL to UI → user opens in browser → completes login → copies code from `oauth/code/callback`.
4. UI sends the code back to Octopus; Octopus writes it to the subprocess stdin (the CLI's prompt expects exactly the pasted code).
5. CLI completes token exchange, prints `sk-ant-…` token, exits.
6. Octopus captures the token from stdout, stores it as a credential (the existing encrypted-API-key storage works as-is — the OAuth token IS an API key, just long-lived).
7. Session spawn unchanged: `ANTHROPIC_API_KEY=<token>` on the subprocess env.

### What's still unverified

- Whether `claude setup-token` will actually accept a piped/PTY non-tty interactive input gracefully (Ink might require a real TTY — Python's `pty` module solves this).
- Exact regex pattern that reliably extracts the authorize URL from the styled Ink output.
- Exact format of what stdout looks like when the token is displayed (so we know what to grep for).
- Whether `setup-token` requires a TTY check that PTY satisfies.

Phase OAuth-7 will close these — that step asks the user to walk through one real login on their machine and we adjust based on what we actually observe.
