# Future Features

---

## 1. Multi-Backend Support via Direct CLI Subprocess (Claude Code + Codex)

**Priority**: High (foundation for Codex support, Connectors, AskUserQuestion done properly)
**Affected**: `server/session_manager.py`, `server/config.py`, new `server/backends/` package, eventually retires the `claude-code-sdk` dependency entirely

### Problem

Octopus is tightly coupled to the Claude Code Python SDK. The `Session` dataclass holds an SDK client (`_client: ClaudeSDKClient`), and `_run_claude()` imports SDK message types (`AssistantMessage`, `ToolUseBlock`, etc.) directly. The SDK is pre-1.0 (`v0.0.25`), already drifts from the CLI (we patch `message_parser.py` locally), and — most importantly — hides the JSONL wire protocol from us. That hiding has concrete costs:

- Built-in tools like `AskUserQuestion` have no clean integration path (today's `PermissionResultDeny`-as-answer is a semantic hack; see section 6).
- Adding a second backend (Codex) would create a *second* lifecycle/error model since Codex has no official Python SDK and must be subprocess-wrapped anyway.
- Any feature the SDK doesn't expose (custom permission protocols, mid-stream injection) is blocked until upstream catches up.

### Direction

**Drop the SDK. Wrap both Claude Code and Codex via direct CLI subprocess + JSONL.** One abstraction, two concrete backends that share the same lifecycle, error handling, and event-normalization machinery. The CLI's JSONL protocol is the actually-stable interface (the SDK is just a thin wrapper over it).

This is also what the official Claude Code TypeScript SDK does, and what the official Codex TypeScript SDK does. We're aligning Octopus with the upstream pattern instead of inheriting Python-SDK-specific quirks.

### Backend comparison (informational)

Both backends are subprocess-driven. Differences live below the `BackendBase` interface:

| Aspect | Claude Code CLI (direct subprocess) | Codex CLI (direct subprocess) |
|---|---|---|
| Binary | `claude` (npm `@anthropic-ai/claude-code`) | `codex` (npm `@openai/codex`) |
| Stream flags | `--input-format=stream-json --output-format=stream-json` | `exec --json` (or `resume … --json`) |
| Session unit | Streaming stdin/stdout JSONL | One-shot exec per turn, JSONL on stdout |
| Event vocabulary | `user`, `assistant`, `system`, `result`, plus control protocol | `thread.started`, `turn.started/completed/failed`, `item.started/updated/completed` |
| Content blocks | `text`, `thinking`, `tool_use`, `tool_result` | Items: agent messages, reasoning, commands, file changes, MCP |
| Session resume | `--resume <id>` (or `--continue`) | `codex resume <id>` or `--last` |
| Permissions | `--permission-mode` + control-protocol `can_use_tool` callbacks | `--sandbox` modes (read-only / workspace-write / full-access) |
| Per-tool callback | Yes (control protocol) | No — sandbox-level only |
| Cost tracking | `result` event's `total_cost_usd` | Token usage in event metadata |
| Auth | `claude login` writes `~/.claude/auth.json`, or env `ANTHROPIC_API_KEY` | `codex login` (OAuth) or env `OPENAI_API_KEY` |
| Built-in `AskUserQuestion` | Yes — currently undefined behavior in headless mode | No (no native equivalent) |

### Proposed Design

#### A. Package layout

```
server/backends/
    __init__.py            # exports BackendBase, BackendEvent, get_backend()
    base.py                # BackendEvent dataclass + BackendBase ABC
    subprocess_jsonl.py    # Shared subprocess driver (spawn / stdout reader / stdin writer / shutdown)
    claude_code.py         # ClaudeCodeBackend(SubprocessJsonlBackend)
    codex.py               # CodexBackend(SubprocessJsonlBackend)
```

#### B. Base abstraction

```python
# server/backends/base.py
@dataclass
class BackendEvent:
    """Normalized event emitted by any backend."""
    type: str          # "text", "tool_use", "tool_result", "result", "error", "question_request"
    content: str | None = None
    tool_name: str | None = None
    tool_input: Any = None
    tool_use_id: str | None = None
    is_error: bool = False
    cost: float | None = None
    session_id: str | None = None  # backend-specific session/thread ID for resume
    raw: dict | None = None        # original wire event, for debugging / future-proofing

class BackendBase(ABC):
    name: str  # "claude-code" | "codex"

    @abstractmethod
    async def start(self, prompt: str, working_dir: str, resume_id: str | None = None) -> None: ...

    @abstractmethod
    def stream(self) -> AsyncIterator[BackendEvent]: ...

    @abstractmethod
    async def stop(self) -> None: ...

    async def interrupt(self) -> None:
        """Best-effort cancel. Default impl just calls stop()."""
        await self.stop()

    async def answer_question(self, question_id: str, answer_text: str) -> bool:
        """Provide an answer for a pending AskUserQuestion. Default: not supported."""
        return False
```

#### C. Shared subprocess driver

`SubprocessJsonlBackend` (in `subprocess_jsonl.py`) is the shared parent for both backends. It owns:
- `asyncio.create_subprocess_exec` lifecycle (spawn, terminate, kill if hangs)
- An stdout reader task that decodes one JSON object per line and pushes onto an `asyncio.Queue`
- An stderr reader (for debug logs / error surfacing)
- An stdin writer (used by Claude for streaming prompts and control-protocol responses)
- Graceful + forceful shutdown with timeouts
- An abstract `_normalize(raw: dict) -> BackendEvent | None` hook that subclasses implement

Subclasses only need to: build the command-line args in `start()`, and translate raw events in `_normalize()`. Lifecycle and I/O are shared.

#### D. ClaudeCodeBackend (CLI-direct, replaces SDK)

```python
class ClaudeCodeBackend(SubprocessJsonlBackend):
    name = "claude-code"
    binary = "claude"

    def build_args(self, prompt, working_dir, resume_id):
        args = [self.binary, "--input-format=stream-json", "--output-format=stream-json",
                "--permission-mode=default"]
        if resume_id:
            args += ["--resume", resume_id]
        return args, {"cwd": working_dir}

    async def _send_prompt(self, prompt: str) -> None:
        # Claude reads JSONL on stdin; first send the user turn.
        msg = {"type": "user", "message": {"role": "user", "content": prompt}}
        await self._write_stdin(json.dumps(msg) + "\n")

    def _normalize(self, raw: dict) -> BackendEvent | None:
        # Map raw CLI events (user/assistant/system/result/control_request) to BackendEvent.
        # Exact field names come from the Phase 1b protocol-notes doc.
        ...
```

The control-protocol handling (e.g. CLI asks host for `can_use_tool` decision; host responds via stdin) is implemented here, not in `session_manager`. That's where AskUserQuestion can be intercepted *correctly* — by detecting the tool name in the control request and routing it through the same `answer_question` future that the UI resolves.

#### E. CodexBackend

```python
class CodexBackend(SubprocessJsonlBackend):
    name = "codex"
    binary = "codex"

    def build_args(self, prompt, working_dir, resume_id):
        if resume_id:
            return [self.binary, "resume", resume_id, "--json"], {"cwd": working_dir}
        return [self.binary, "exec", "--json", "--sandbox", "workspace-write", prompt], {"cwd": working_dir}

    async def _send_prompt(self, prompt: str) -> None:
        # Codex takes the prompt as a CLI argument (no stdin streaming on exec).
        pass

    def _normalize(self, raw: dict) -> BackendEvent | None:
        # Map item.started/updated/completed + turn.* → BackendEvent
        ...
```

#### F. Session and database changes

```python
@dataclass
class Session:
    id: str
    name: str
    working_dir: str
    backend: str = "claude-code"           # "claude-code" | "codex"
    backend_session_id: str | None = None  # rename of claude_session_id
    ...
```

Schema migration:
- Add column `sessions.backend TEXT NOT NULL DEFAULT 'claude-code'`
- Rename `sessions.claude_session_id` → `sessions.backend_session_id` (or add the new column and migrate values, then drop the old one)

#### G. SessionManager changes

Replace `_run_claude()` with a backend-agnostic `_run_backend()` that delegates to `session.backend_instance.stream()`. All Claude-specific imports leave `session_manager.py`.

#### H. Frontend changes

- Session creation dialog: backend selector (Claude Code / Codex)
- Session list: small backend badge per session
- Chat UI is unchanged — events are already normalized

### Implementation order

1. **Phase 1a** ✅ — Update this doc.
2. **Phase 1b** — Run the CLI standalone, capture sample JSONL for normal flows + AskUserQuestion + tool use + resume. Commit findings to `docs/cli-protocol-notes.md`. This is the *source of truth* for what `_normalize()` has to handle.
3. **Phase 1c** — Build `BackendBase`, `BackendEvent`, and `SubprocessJsonlBackend`. Unit-test the shared driver against a tiny fake CLI script that emits known JSONL.
4. **Phase 1d** — Build `ClaudeCodeBackend` parallel to the existing SDK path. Per-session `backend` field selects which path runs (`claude-code-sdk` legacy vs `claude-code-cli` new). Run both against the same prompts and compare outputs.
5. **Phase 1e** — Cut over: `session_manager` uses only the backend interface. Delete the SDK code path, drop `claude-code-sdk` from `pyproject.toml`, delete the local `message_parser.py` patch.
6. **Phase 1f** — Add `CodexBackend` (depends on Codex CLI being installable in the dev environment).
7. **Phase 1g** — Frontend backend selector + per-session badge.

### Risks

- **Wire format drift**: the CLI's JSONL output format isn't fully documented. We freeze our expectations in `cli-protocol-notes.md` and add a regression test that replays the recorded JSONL through `_normalize()`. CLI upgrades that change fields will fail this test loudly.
- **Control protocol complexity**: `can_use_tool`, hooks, and MCP all live in a side-channel control protocol that Phase 1b needs to characterize. If it's too gnarly we ship Phase 1e without those features and add them incrementally.
- **Codex feature gap**: no per-tool approval, no hooks. `BackendBase.answer_question` and similar will be no-ops for Codex. The Frontend should hide the per-tool approval UI when the active session is Codex.
- **Auth surfaces differ**: Claude Code uses `~/.claude/auth.json` or `ANTHROPIC_API_KEY`; Codex uses `codex login` or `OPENAI_API_KEY`. Covered by section 7 below.
- **Tests must run without either CLI installed**: backend unit tests use a fake CLI script (a Python file that emits canned JSONL). Only the e2e suite hits the real CLI.

Sources:
- [Codex CLI repo](https://github.com/openai/codex)
- [Codex TypeScript SDK README](https://github.com/openai/codex/blob/main/sdk/typescript/README.md) — confirms subprocess+JSONL is the official wrapping pattern
- Local Claude Code CLI (the `claude` binary in this dev environment) — empirically characterized in `docs/cli-protocol-notes.md`

---

## 2. Email Integration (Read & Write)

**Priority**: Medium
**Affected**: new `server/bridges/email.py`, `server/config.py`

### Context

Allow Octopus sessions to read and compose emails. This extends the bridge pattern already used for Telegram.

### Possible Approach

- **Reading**: IMAP integration to poll/fetch emails, or Gmail API via OAuth
- **Writing**: SMTP for sending, or Gmail API for drafts/send
- Bridge implementation: new `EmailBridge` class extending the existing `Bridge` base
- Config: IMAP/SMTP credentials or OAuth tokens in `Settings`
- Use cases:
  - Forward incoming emails to a session for summarization/triage
  - Have Claude draft replies that the user can review before sending
  - Scheduled email digests (combines with the scheduling feature already shipped)

---

## 3. Eye-Friendly Background Theme

**Priority**: High (low effort, high quality-of-life)
**Affected**: `web/src/index.css`

### Problem

The current dark palette uses `--bg: #0d1117` (GitHub-dark). After long sessions it reads as near-black against bright text and tool-use blocks, causing eye strain. Pure-black-on-bright-white contrast is harsh on OLED/IPS panels at full brightness.

### Goal

Replace the near-black canvas with a softer, slightly warmer dark tone that keeps enough contrast for code blocks and tool output, but reduces glare on extended use.

### Possible Approach

Edit the `:root` palette in `web/src/index.css`. Candidates (any of these is a reasonable default — pick one and ship):

| Variable | Current | Proposed |
|---|---|---|
| `--bg` | `#0d1117` | `#1b1f24` (slightly lighter, neutral) — or `#1f1d1a` (warm) |
| `--bg-secondary` | `#161b22` | `#23282f` |
| `--bg-tertiary` | `#21262d` | `#2d333b` |
| `--text` | (current) | bump down a touch to ~`#d1d5db` so contrast ratio drops from harsh-white to comfortable |

Keep the accent (green) and red/amber semantic colors as-is.

### Optional: per-user theme toggle

If a single palette change isn't enough, add a theme switcher:
- New `theme: "dim" | "dark" | "midnight"` field in zustand store, persisted to localStorage
- CSS uses `[data-theme="dim"]` selectors on `<body>`
- Settings dropdown in the sidebar (or a small icon button next to Logout) toggles

MVP: just change the variables. Theme switcher is a nice-to-have.

### Verification

- Visual check on the longest chat view in the app
- Contrast ratio against `--text`: aim for 7:1 minimum for body text (WCAG AAA), 4.5:1 for code/secondary
- Re-run e2e tests — none should depend on exact pixel colors, but confirm Playwright snapshots (if any) are updated

### Scope cuts (deferred)

- Light theme
- System-preference detection (`prefers-color-scheme`)
- Per-component palette overrides

---

## 4. In-App Markdown Reader

**Priority**: Medium
**Affected**: new `web/src/components/FileViewer.tsx`, new endpoint in `server/routers/sessions.py` (or new `server/routers/files.py`), `web/src/components/MessageBubble.tsx`

### Context

Claude often produces or edits markdown files in the working directory (READMEs, plans, notes). Today the user has to switch to a separate editor / terminal to read them. A built-in reader lets the user open `.md` files (and other text files) directly inside Octopus right after Claude writes them.

### Goal

When Claude finishes a tool use that wrote/edited a file in the session's `working_dir`, surface a "View" button on that tool block. Clicking opens a side panel or modal that fetches the file content and renders it (markdown → rendered HTML via the same `react-markdown` + `remark-gfm` already used for chat; other text → plain monospace).

### Possible Approach

#### A. Server endpoint

```
GET /api/sessions/{session_id}/files?path=<relative_or_absolute>
```

- Resolve the path against the session's `working_dir`
- **Security**: reject paths outside `working_dir` (use `os.path.realpath` + `commonpath` check) — same risk model as a code-server proxy. Refuse symlinks that escape the dir.
- Limit response size (e.g., 1 MiB) — anything bigger returns a 413 with a "file too large" hint
- Detect content type by extension; mark `.md`, `.txt`, `.py`, `.ts`, etc. as text. Refuse binary
- Auth: standard `Authorization: Bearer <token>`

Response:
```json
{
  "path": "docs/plan.md",
  "size": 12034,
  "mime": "text/markdown",
  "content": "# Plan\n..."
}
```

#### B. Frontend component

New `FileViewer.tsx` — a slide-in panel (right side, ~40% width on desktop, full-screen modal on mobile) with:
- Header: file path, close button, copy-path button
- Body:
  - For `.md`: `<ReactMarkdown remarkPlugins={[remarkGfm]}>` (reuse the chat config)
  - For other text: `<pre><code>` with monospace
- Optional: line numbers for code

State lives in the zustand store: `viewerOpen`, `viewerPath`, `viewerContent`.

#### C. Triggering from chat

- In `MessageBubble.tsx`, when rendering a tool_use block for `Write`, `Edit`, `MultiEdit`, or `NotebookEdit`, extract `file_path` from `tool_input` and add a small "View" button next to the existing collapse arrow
- Clicking calls `openViewer(path)` which fetches and shows the content
- Also: a top-level "Browse files" button in the chat header to open an arbitrary path under `working_dir` (deferred — MVP is "open the file Claude just touched")

#### D. Refresh behavior

- The viewer caches the last-fetched content
- A "Reload" button re-fetches from disk (since Claude may edit again)
- Optional: auto-reload when a new tool_use targeting the same path lands

### Scope cuts (deferred)

- Directory tree / file browser
- Inline editing
- Diff viewer (show changes vs previous version)
- Image preview
- Server-Sent Events for live file watching
- Syntax highlighting for code (markdown is the focus; code can come later via `react-syntax-highlighter`)

### Risks

- **Path traversal**: must validate `path` against `working_dir` strictly. Add a backend test for `../../etc/passwd` style attacks
- **Large files**: enforce the size cap server-side; truncate gracefully
- **Binary files**: detect via null bytes in first 8 KiB or by extension allowlist; refuse with a clear message
- **Working directory drift**: Claude may `cd` during a session; the file path it writes may be relative to a subdirectory. Resolve relative paths against `working_dir`, not the session's logical cwd at message time

---

## 5. Sidebar Reorganization: Sessions / Schedules / Connectors

**Priority**: High (foundational — unlocks future integrations)
**Affected**: `web/src/App.tsx`, `web/src/components/SessionList.tsx`, `web/src/components/ScheduleList.tsx`, new `web/src/components/ConnectorList.tsx`, new `server/routers/connectors.py`, generalization of `server/bridges/`

### Goal

Reshape the sidebar around three top-level sections:

1. **Sessions** — current session list (unchanged)
2. **Schedules** — current scheduled-task list (already exists, currently nested differently)
3. **Connectors** — *new* — manage integrations (Email, GitHub, Lark, Telegram, …) that can be attached to sessions to let them do "interesting things" beyond plain chat

### Why now

Connectors generalize the existing `server/bridges/` pattern (today Telegram is the only one, configured via env vars). Surfacing it in the UI lets users:
- Define a connector once (credentials, scope, config)
- Attach it to any session, so Claude in that session can read/send via that channel
- Configure inbound routing (incoming events become user messages in a chosen session) and outbound capabilities (Claude can act through the connector)

Once the section exists, adding Email (feature #2), GitHub, Lark, etc. becomes incremental work rather than each one requiring its own UI plumbing.

### Design forks (resolve during planning)

- **Connector role**: outbound-only (tools) vs inbound-only (event routing) vs unified — affects data model and UX
- **Scoping**: global definitions attached per-session vs fully per-session config — affects credential management
- **Relationship to MCP**: thin UI over Claude Code SDK's MCP config vs a separate, bridge-based system — affects whether inbound flows are supported

A reasonable starting point: unified (both directions), globally defined with per-session attachment, kept separate from MCP (since MCP doesn't model inbound flows well and the bridge pattern already proves this works for Telegram).

### Implementation sketch

1. Generalize `server/bridges/` into a connector framework (rename/refactor as needed). Each connector has `name`, `type`, `config`, `enabled`, and an attached-sessions list
2. New REST endpoints under `/api/connectors` for CRUD + per-session attach/detach
3. Sidebar gains a 3-tab switcher; each tab renders its own list component
4. Connector detail panel: type-specific config form (email creds, GitHub PAT, etc.)
5. Session view shows a small "Connectors" chip with the attached integrations
6. Migrate the existing Telegram bridge into the new framework

### Scope cuts (deferred)

- OAuth flows (start with API keys / app passwords)
- Per-connector tool allowlists in the UI (rely on the connector itself to gate what it exposes)
- Live event log view per connector

---

## 6. Per-Backend Auth Management in the WebUI

**Priority**: High (depends on feature 1)
**Affected**: `web/src/components/SettingsPanel.tsx` (new), `server/routers/auth_backends.py` (new), `server/database.py` (schema migration)

### Goal

Today auth for Claude Code lives outside Octopus (env var `ANTHROPIC_API_KEY` or `~/.claude/auth.json`). Once we support Codex too, users need a single in-app place to:
- See which backends are authenticated
- Sign in / sign out per backend
- Override per-session (e.g., one session uses a personal Claude account, another uses a team OpenAI key)

### Approach

#### A. Storage

New table `backend_credentials`:

```sql
CREATE TABLE backend_credentials (
    id TEXT PRIMARY KEY,
    backend TEXT NOT NULL,       -- "claude-code" | "codex"
    label TEXT NOT NULL,         -- "Personal Anthropic", "Work OpenAI"
    auth_type TEXT NOT NULL,     -- "api_key" | "oauth"
    secret_encrypted TEXT NOT NULL,
    created_at TEXT NOT NULL
);
```

Secrets encrypted at rest with a key derived from `OCTOPUS_AUTH_TOKEN` (or a dedicated `OCTOPUS_SECRET_KEY`). Optional `session.credential_id` foreign key to override per-session; default falls back to the first credential of the right backend.

#### B. REST endpoints

```
GET    /api/auth/backends                 # list credentials (label + backend only, never the secret)
POST   /api/auth/backends                 # create
PATCH  /api/auth/backends/{id}            # rename / rotate secret
DELETE /api/auth/backends/{id}            # remove
POST   /api/auth/backends/{id}/test       # verify credential still works
```

#### C. Backend integration

`BackendBase.start()` gains an optional `credential: Credential | None` parameter. The concrete backend either:
- Sets the appropriate env var on the subprocess (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`)
- Writes a temporary auth file the CLI will read (for OAuth tokens — for `claude` that means materializing the equivalent of `~/.claude/auth.json` in a per-session config dir)

`SessionManager` resolves the credential when starting a session run and passes it through.

#### D. Frontend

- New "Settings" panel in the sidebar (or footer next to Logout) with two tabs: *Claude Code* and *Codex*
- Each tab lists configured credentials, with "Add", "Test", "Rename", "Delete" actions
- Session creation form gets an optional "Use credential" dropdown (defaults to first matching credential)
- Visual indicator on a session if its credential test fails (badge in the session list)

### Scope cuts

- Multi-user / team auth (single-user model for now — the credential table is just "which sets of keys this Octopus instance can use")
- OAuth flow inside Octopus for Codex (start with paste-the-key UX; OAuth comes later)
- Key rotation reminders / expiry warnings

---

## 7. AskUserQuestion: Native Handling via Direct CLI Control Protocol

**Priority**: Medium (cosmetic cleanup of a working feature; depends on feature 1)
**Affected**: `server/backends/claude_code.py`, `server/session_manager.py`

### Today's state

Shipped: `can_use_tool` callback intercepts `AskUserQuestion`, broadcasts a `question_request` event, awaits the user's answer through a Future, returns `PermissionResultDeny(message=answer)`. The model reads the deny message as the answer. Works in practice but is a semantic hack — the answer is delivered via a "denial reason" channel.

### After feature 1 lands

With direct CLI ownership we know exactly how the CLI sources answers (Phase 1b will have documented this in `cli-protocol-notes.md`). Three likely paths:

1. **The CLI reads the answer from stdin** — our subprocess driver owns stdin, so we write the answer ourselves when we see the `tool_use` event for `AskUserQuestion`. Claude sees a normal positive tool_result. Clean.
2. **The CLI uses a control-protocol message asking the host for the answer** — `ClaudeCodeBackend` already handles control messages (for `can_use_tool`); we add another handler that responds with the answer. Also clean.
3. **The CLI tries to render its own UI and fails in headless mode** — we keep `disallowed_tools=["AskUserQuestion"]` and register our own equivalent as an MCP tool (or just inject a system prompt that asks Claude to use a custom mechanism). Slightly less clean but still better than the deny-message hack.

Phase 1b answers which path applies, and feature 7 then implements whichever is correct.

### Migration

Existing UI (`QuestionPrompt.tsx`, store state, `answer_question` WS message) stays unchanged — only the backend wire-up changes. The `BackendEvent` for `question_request` is already in the abstraction; the difference is *how* the backend produces it and *how* the answer flows back to the CLI.

### Scope cuts

- No new UI work (the form already exists)
- No backwards compatibility with the SDK path (feature 1 already retires it)
