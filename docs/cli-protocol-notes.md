# Claude Code CLI JSONL Protocol Notes

Empirical capture of the `claude` CLI's stream-json protocol, used as
the source of truth for `ClaudeCodeBackend` in
`server/backends/claude_code.py`.

**Captured against**: `claude` v2.1.143.

**Reproduce**: `tests/_fixtures/fake_claude_cli.py` scripts the same
shape; `tests/test_backend_claude_code.py` exercises the normalizer
against it. For the real binary, `tests/test_backend_claude_code_real.py`
runs against `claude` if it's on `$PATH`.

**Note on revision**: this doc was rewritten after the VM0-shape
refactor (commit `5815560`). The old shape used
`--input-format=stream-json` + `--permission-prompt-tool=stdio` and
drove a bidirectional control protocol over stdin; that path is gone.
The refactor and the recovery loop that sits on top of it are
documented in `docs/2026-05-18-bg-pipeline-hardening.md` §2.

## Invocation

```
claude --print \
       --output-format=stream-json \
       --verbose \
       --dangerously-skip-permissions \
       --disallowedTools AskUserQuestion \
       --mcp-config '<inline JSON>' \
       --append-system-prompt '<Octopus addendum>' \
       [--resume <session-id>] \
       [--include-partial-messages] \
       -- \
       <prompt as a single positional argument>
```

Notes per flag:

- `--print`: one-shot, non-interactive turn. We drive multi-turn via
  `--resume` + re-spawn.
- `--output-format=stream-json`: parse events from stdout. **NOT** the
  input format — we don't set `--input-format` at all (defaults to
  `text`, which makes the CLI take the prompt as a positional argv).
- `--verbose`: **required** alongside `--output-format=stream-json`
  under `--print` (CLI errors otherwise).
- `--dangerously-skip-permissions`: bypass per-tool permission checks.
  This is intentionally permissive — Octopus is the only thing
  spawning these subprocesses on the user's behalf, so the user
  already trusts the call. The previous shape
  (`--permission-prompt-tool=stdio` + host callback) gave us nothing
  we needed and was on the failure surface for the CLI
  premature-exit bug; see `docs/2026-05-18-bg-pipeline-hardening.md`
  §2.
- `--disallowedTools AskUserQuestion`: prevents the model from
  calling the built-in AUQ. We provide `mcp__ask__user` (see below)
  as the replacement.
- `--mcp-config <JSON>`: registers our in-process MCP servers
  (`bg`, `ask`). The CLI accepts either a file path or an inline JSON
  string; we use inline.
- `--append-system-prompt <text>`: short addendum teaching the model
  about `bg_run` and `ask_user`. The full text lives in
  `claude_code.py:_OCTOPUS_SYSTEM_PROMPT`.
- `--resume <session-id>`: continue an existing conversation.
- `--`: terminator for option parsing. The single positional
  argument that follows is the user prompt.

What we **do not** set, and why:

- `--input-format=stream-json` — was load-bearing on the failure
  surface for the CLI premature-exit bug
  (`docs/2026-05-18-bg-pipeline-hardening.md` §2). With the default
  text input, we pass the prompt as argv and never write to stdin.
- `--permission-prompt-tool=stdio` — replaced by `--dangerously-skip-permissions`.
- `--permission-mode default` — moot once skipped.
- `--no-session-persistence` — we *want* persistence so `--resume`
  works across turns.

## Wire format

- One JSON object per line on **stdout**.
- All events share a `type` field; many also carry `session_id` and `uuid`.
- **stderr** is used for warnings and fatal errors (not used for
  events in normal operation).
- **stdin** is **not used** by Octopus under the VM0 shape. The prompt
  is on argv; control responses don't exist (no control protocol).
  Octopus's `claude_code.py:send_initial_prompt` is a no-op for
  exactly this reason.

## Event types observed on stdout

| Type | Subtype | Meaning |
|---|---|---|
| `system` | `init` | First event: full server config — `session_id`, `tools[]`, `model`, `permissionMode`, `cwd`, `slash_commands[]`, `agents[]`, `apiKeySource`, `claude_code_version`, `memory_paths`, etc. |
| `system` | `status` | Informational status pings (`requesting`, `responding`, …). Safe to ignore for chat UX. |
| `rate_limit_event` | — | Rate-limit status snapshot. Informational. |
| `assistant` | — | Model message. `message.content[]` carries content blocks — see below. |
| `user` | — | Tool results echoed back by the CLI's built-in tool runner (Bash, Edit, etc.). `message.content[]` carries `tool_result` blocks. |
| `result` | `success` or `error_during_execution` | Terminal event for one turn. Includes `total_cost_usd`, `duration_ms`, `num_turns`, `session_id`, `usage`, `permission_denials[]`, `terminal_reason`. |
| `stream_event` | — | Only when `--include-partial-messages` is set. Wraps an Anthropic API stream chunk. We don't enable this flag today. |
| `control_response` | — | Used to exist for the SDK control protocol; under the VM0 shape it shouldn't appear in normal operation. `claude_code.py:_handle_control_response` is a vestigial no-op for safety. |
| `control_request` | — | Same — vestigial. `_handle_control_request` is a no-op. |

## Content blocks (inside `assistant.message.content[]` and `user.message.content[]`)

| Block `type` | Fields | Notes |
|---|---|---|
| `text` | `text` | Plain assistant text. |
| `thinking` | `thinking`, `signature` | Hidden reasoning. We persist for resume but don't render. |
| `tool_use` | `id`, `name`, `input`, `caller` | Model invoking a tool. `caller.type` is `direct` for top-level calls. |
| `tool_result` | `tool_use_id`, `content`, `is_error` | Result echoed by the CLI. Sits inside a `user` event. `content` is usually a string but for image-returning tools (e.g. `Read` on a PNG) it's a list of content blocks like `[{type:"image", source:{...}}]`. |

**Important quirk:** for multi-block assistant messages, the CLI emits
**one `assistant` event per content block**, all sharing the same
`message.id` and `request_id`. So a thinking + text + tool_use turn →
3 separate stdout lines. The normalizer treats them as independent
events and does not try to assemble them.

## Tool flow (normal happy path)

```
assistant {content: [{type: "tool_use", id: X, name: "Bash", input: {...}}]}
user      {content: [{type: "tool_result", tool_use_id: X, content: "...", is_error: false}]}
assistant {content: [{type: "text", text: "..."}]}
result    ...
```

The CLI runs built-in tools (Bash, Read, Edit, Write, Glob, Grep, …)
itself — no host involvement. We only see the `tool_use` and
`tool_result` events on stdout.

## MCP tools we register

`--mcp-config` registers our in-process MCP servers, each launched by the
CLI as its own subprocess (children of `claude`, grandchildren of the
FastAPI process). The model sees them as `mcp__<server-key>__<tool-fn>`:

| MCP tool | Purpose | How it talks to Octopus |
|---|---|---|
| `mcp__bg__run(command, description?)` / `cancel` / `list` | Fire-and-forget shell commands that outlive the per-turn `claude --print` | POSTs to `/api/sessions/{id}/bg-tasks`; FastAPI's `BgTaskManager` owns the subprocess and injects a follow-up turn on completion. |
| `mcp__ask__user(questions)` | Replaces the built-in `AskUserQuestion` | POSTs to `/api/sessions/{id}/questions` and HTTP-long-polls the answer. Frontend renders the QuestionPrompt form; user's submit sets an `asyncio.Event` that unblocks the long-poll. |

The in-app file viewer used to be a third MCP server (`mcp__viewer__show_file`)
that the model could call. It was dropped: the agent doesn't know whether a
human is at the screen, so popping a modal proactively is presumptuous.
`/showme` is now intercepted client-side and resolved through a dedicated REST
endpoint (`server/showme_ai.py`).

Each MCP subprocess gets these env vars from `build_args`:

```
OCTOPUS_API_BASE     http://127.0.0.1:{settings.port}
OCTOPUS_AUTH_TOKEN   the bearer the rest of the API uses
OCTOPUS_SESSION_ID   so callbacks attribute to the right session
PYTHONPATH           = repo root, so the MCP server's `from server.…` imports resolve
```

## Resume

`--resume <session-id>` works after the previous subprocess has fully
exited (state persists in `~/.claude/sessions/`). The resumed run
emits a new `system.init` event but keeps the same `session_id`.
Confirmed: the new turn has full context from the prior conversation.

We mint our own session id by capturing the first `system.init`'s
`session_id` and storing it on the Octopus session row as
`claude_session_id`.

`--continue` resumes the most recent conversation for the current
directory. We don't use it — too cwd-coupled.

## Interrupts

`SubprocessJsonlBackend.stop()`: closes stdin → 2 s grace → SIGTERM →
2 s grace → SIGKILL. That's the entire interrupt mechanism. There's
no graceful interrupt control_request to send first — there's no
stdin protocol to send it over.

`ClaudeCodeBackend.interrupt()` is therefore just an alias for
`stop()`. Any in-flight tool work in MCP-server children dies with
their parent process group.

## Known bug — premature exit on stdout

The CLI's `--print --resume` loop has a bug where, at large input
context and for some tool-result shapes (we have observed it on text
results > 50 KB, and reliably on image results at ~900K input
tokens), the CLI runs the tool, persists the result to its private
jsonl at `~/.claude/projects/...`, but **never emits the
corresponding `user` event on stdout**. Octopus's stdout reader hits
EOF and treats the turn as ended; the user sees the chat go silent
with the tool's result missing.

The post-mortem and the mitigation that shipped live in
`docs/2026-05-18-bg-pipeline-hardening.md` §2. Short version:

- Independent of the input-format / permission-prompt path (the
  VM0-shape refactor reduced frequency but did not eliminate).
- Worth filing upstream with Anthropic.
- Octopus-side mitigation: `session_manager._run_backend` is a loop
  that tracks `saw_result` / `saw_tool_use` across the event
  stream. If the stream ends without a `result` after a `tool_use`
  AND we have a resume id, it respawns the CLI once with prompt
  `"continue"`. Bounded by `_MAX_RECOVERY_ATTEMPTS = 1`; the
  recovery turn surfaces as a system marker
  `(auto-resumed after CLI exited mid-turn)`.

## What this means for `ClaudeCodeBackend`

Under the VM0 shape, the implementation is small:

1. Spawn `claude` with the flags above, prompt as argv.
2. Don't touch stdin at all — `send_initial_prompt` is a no-op.
3. Read stdout line-by-line, parse each as JSON, route by `type`.
4. Per-block emission: forward each `assistant.content[i]` block as
   its own normalized `BackendEvent` (`text` / `tool_use` /
   `thinking`).
5. For `user.tool_result`, forward as `BackendEvent(type="tool_result")`.
6. For `result`, capture `session_id` (for resume) + `total_cost_usd`
   + `duration_ms`; close the per-turn iterator.
7. Graceful shutdown: close stdin → wait for `result` → terminate;
   force-kill after timeout. Same as before — the close-stdin step is
   still there for cleanliness even though we never wrote to stdin.

---

## OAuth / login surface (CLI v2.1.143)

(Unchanged from the original notes — the OAuth path is independent of
the loop refactor.)

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
