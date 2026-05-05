# Future Features

---

## 1. Multi-Backend Support (Claude Code + Codex)

**Priority**: Low
**Affected**: `server/session_manager.py`, `server/config.py`, new `server/backends/` package

### Problem

Octopus is tightly coupled to the Claude Code SDK. The `Session` dataclass has Claude-specific fields (`claude_session_id`, `_client: ClaudeSDKClient`), and `_run_claude()` directly imports and uses Claude SDK types (`AssistantMessage`, `ToolUseBlock`, `ResultMessage`, etc.).

To support OpenAI Codex (or other coding agents in the future), we need an abstraction layer that normalizes the differences between backends.

### SDK Comparison

**Claude Code SDK** is an official Anthropic package (`claude-code-sdk` on PyPI).

**Codex SDK** — OpenAI only provides an official **TypeScript SDK** (`@openai/codex-sdk` on npm). There is **no official Python SDK** from OpenAI. The `openai-codex-sdk` package on PyPI is a **third-party community project**. Several community alternatives exist:
- [comfuture/codex-sdk-python](https://github.com/comfuture/codex-sdk-python)
- [yor-dev/python-codex-sdk](https://github.com/yor-dev/python-codex-sdk)
- [spdcoding/codex-python-sdk](https://github.com/spdcoding/codex-python-sdk)

An [open issue (#5320)](https://github.com/openai/codex/issues/5320) on the Codex repo proposes an official Python SDK, but it has not been accepted.

**For Octopus integration, we have two options:**
1. Use a community Python SDK (risk: may break or become unmaintained)
2. Wrap the Codex CLI directly via subprocess + JSONL parsing (same approach the official TypeScript SDK uses internally — more stable, no third-party dependency)

Option 2 is recommended since the Codex CLI's `--json` output format is the stable interface.

| Aspect | Claude Code SDK (Python, official) | Codex CLI (direct subprocess) |
|---|---|---|
| Package | `claude-code-sdk` (v0.0.25) | `codex` CLI via npm (`@openai/codex`) |
| Interface | Python SDK wrapping CLI subprocess | Direct subprocess with `--json` flag |
| Main class | `ClaudeSDKClient` | N/A — spawn `codex exec --json` |
| Session unit | Async context manager, `client.query()` | `codex exec --json <prompt>` |
| Streaming | `client.receive_response()` → async iterator of typed `Message` | JSONL on stdout: `item.started`, `item.completed`, `turn.completed` |
| Message types | `UserMessage`, `AssistantMessage`, `SystemMessage`, `ResultMessage`, `StreamEvent` | Events: `thread.started`, `turn.started/completed/failed`, `item.started/updated/completed` |
| Content blocks | `TextBlock`, `ThinkingBlock`, `ToolUseBlock`, `ToolResultBlock` | Items: agent messages, reasoning, command executions, file changes, MCP tool calls |
| Session resume | `ClaudeCodeOptions(resume="session_id")` | `codex resume <session-id>` or `--last` |
| Permission model | `permission_mode` + `can_use_tool` callback | `--sandbox` modes: read-only, workspace-write, full-access |
| Cost tracking | `ResultMessage.total_cost_usd` | Token usage in event metadata |
| Tool approval | `SDKControlPermissionRequest` → async callback | Sandbox-level, no per-tool callback |
| Hooks | `PreToolUse`, `PostToolUse`, `Stop`, etc. | Not available |
| MCP servers | Built-in support via `mcp_servers` option | Configured via `~/.codex/config.toml` |

### Proposed Design

#### A. Backend abstraction

Create a `server/backends/` package with a base protocol and per-backend implementations:

```
server/backends/
    __init__.py        # exports BackendBase, get_backend()
    base.py            # abstract base class
    claude_code.py     # Claude Code SDK wrapper
    codex.py           # OpenAI Codex wrapper
```

```python
# server/backends/base.py
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import AsyncIterator, Any

@dataclass
class BackendEvent:
    """Normalized event emitted by any backend."""
    type: str          # "text", "tool_use", "tool_result", "result", "error"
    content: str | None = None
    tool_name: str | None = None
    tool_input: Any = None
    tool_use_id: str | None = None
    is_error: bool = False
    cost: float | None = None
    session_id: str | None = None  # backend-specific session/thread ID for resume
    raw: dict | None = None        # original event for backend-specific handling

class BackendBase(ABC):
    @abstractmethod
    async def start(self, prompt: str, working_dir: str, resume_id: str | None = None) -> None:
        """Start a query. Non-blocking — use stream() to get events."""

    @abstractmethod
    async def stream(self) -> AsyncIterator[BackendEvent]:
        """Yield normalized events from the backend."""

    @abstractmethod
    async def stop(self) -> None:
        """Stop the current query and clean up."""

    @abstractmethod
    async def approve_tool(self, tool_use_id: str) -> bool:
        """Approve a pending tool use. No-op if backend doesn't support per-tool approval."""

    @abstractmethod
    async def deny_tool(self, tool_use_id: str, reason: str = "") -> bool:
        """Deny a pending tool use."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Backend display name (e.g., 'claude-code', 'codex')."""
```

#### B. Claude Code backend

Extract current `_run_claude()` logic into the backend class:

```python
# server/backends/claude_code.py
class ClaudeCodeBackend(BackendBase):
    name = "claude-code"

    async def start(self, prompt, working_dir, resume_id=None):
        opts = ClaudeCodeOptions(
            cwd=working_dir,
            permission_mode="bypassPermissions",
        )
        if resume_id:
            opts.resume = resume_id
        self._client = ClaudeSDKClient(opts)
        await self._client.connect()
        await self._client.query(prompt)

    async def stream(self) -> AsyncIterator[BackendEvent]:
        async for msg in self._client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        yield BackendEvent(type="text", content=block.text)
                    elif isinstance(block, ToolUseBlock):
                        yield BackendEvent(type="tool_use", tool_name=block.name,
                                          tool_input=block.input, tool_use_id=block.id)
                    elif isinstance(block, ToolResultBlock):
                        yield BackendEvent(type="tool_result", content=block.content,
                                          tool_use_id=block.tool_use_id, is_error=block.is_error)
            elif isinstance(msg, ResultMessage):
                yield BackendEvent(type="result", session_id=msg.session_id,
                                  cost=msg.total_cost_usd)

    async def stop(self):
        if self._client:
            await self._client.disconnect()
            self._client = None
```

#### C. Codex backend (direct subprocess, no third-party SDK)

```python
# server/backends/codex.py
import asyncio
import json
import shutil

class CodexBackend(BackendBase):
    name = "codex"

    async def start(self, prompt, working_dir, resume_id=None):
        codex_bin = shutil.which("codex")
        if not codex_bin:
            raise RuntimeError("codex CLI not found — install via: npm install -g @openai/codex")

        cmd = [codex_bin, "exec", "--json", "--sandbox", "workspace-write", prompt]
        if resume_id:
            cmd = [codex_bin, "resume", resume_id, "--json"]

        self._process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=working_dir,
        )

    async def stream(self) -> AsyncIterator[BackendEvent]:
        async for line in self._process.stdout:
            line = line.decode().strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            event_type = event.get("type", "")

            if event_type == "item.completed":
                item = event.get("item", {})
                yield self._normalize_item(item)
            elif event_type == "item.updated":
                # Partial text streaming
                item = event.get("item", {})
                if item.get("type") == "message":
                    yield BackendEvent(type="text", content=item.get("text", ""), raw=event)
            elif event_type == "turn.completed":
                yield BackendEvent(
                    type="result",
                    session_id=event.get("session_id"),
                    raw=event,  # contains usage/token counts
                )
            elif event_type == "turn.failed":
                yield BackendEvent(
                    type="error",
                    content=event.get("error", "Unknown error"),
                    is_error=True,
                    raw=event,
                )

    def _normalize_item(self, item: dict) -> BackendEvent:
        """Convert a Codex item.completed item into a BackendEvent."""
        item_type = item.get("type", "")
        if item_type == "message":
            return BackendEvent(type="text", content=item.get("text", ""), raw=item)
        elif item_type == "command_execution":
            return BackendEvent(
                type="tool_use",
                tool_name="command",
                tool_input={"command": item.get("command", "")},
                tool_use_id=item.get("id"),
                raw=item,
            )
        elif item_type == "file_change":
            return BackendEvent(
                type="tool_use",
                tool_name="file_change",
                tool_input={"path": item.get("path", ""), "diff": item.get("diff", "")},
                tool_use_id=item.get("id"),
                raw=item,
            )
        else:
            return BackendEvent(type="text", content=str(item), raw=item)

    async def stop(self):
        if self._process and self._process.returncode is None:
            self._process.terminate()
            await self._process.wait()
        self._process = None

    async def approve_tool(self, tool_use_id):
        return False  # Codex uses sandbox modes, no per-tool approval

    async def deny_tool(self, tool_use_id, reason=""):
        return False
```

#### D. Session changes

Replace Claude-specific fields with backend-generic ones:

```python
@dataclass
class Session:
    id: str
    name: str
    working_dir: str
    backend: str = "claude-code"           # which backend this session uses
    backend_session_id: str | None = None  # was: claude_session_id
    status: SessionStatus = SessionStatus.idle
    created_at: str = ...
    _backend_instance: BackendBase | None = None  # was: _client
    _message_count: int = 0
    _active_task: asyncio.Task | None = None
    _pending_approvals: dict[str, PendingApproval] = ...
    _lock: asyncio.Lock = ...
```

#### E. SessionManager changes

Replace `_run_claude()` with a backend-agnostic `_run_backend()`:

```python
async def _run_backend(self, session: Session, prompt: str) -> AsyncIterator[dict[str, Any]]:
    backend = get_backend(session.backend)
    session._backend_instance = backend
    try:
        await backend.start(prompt, session.working_dir, session.backend_session_id)
        async for event in backend.stream():
            msg = self._event_to_message(event)
            await self._persist_message(session, msg)
            yield self._event_to_ws(session.id, event)

        # Update resume ID from the last result event
        if event.type == "result" and event.session_id:
            session.backend_session_id = event.session_id
            if self.db:
                await self.db.update_session_field(
                    session.id, backend_session_id=event.session_id
                )
    finally:
        await backend.stop()
        session._backend_instance = None
```

#### F. Configuration

```python
# config.py
class Settings(BaseSettings):
    ...
    default_backend: str = "claude-code"  # "claude-code" | "codex"
    codex_model: str | None = None        # optional model override for codex
```

#### G. Frontend changes

- Session creation dialog: add backend selector dropdown (Claude Code / Codex)
- Session list: show which backend each session uses (small badge/icon)
- The chat UI itself stays the same — `BackendEvent` normalizes the output

#### H. Database migration

Rename `claude_session_id` column to `backend_session_id`. Add `backend` column to sessions table.

### Implementation Order

1. Create `server/backends/base.py` with the abstract interface
2. Create `server/backends/claude_code.py` — extract current `_run_claude()` logic into the backend class
3. Refactor `session_manager.py` to use the backend abstraction (no behavior change yet)
4. Run all tests — everything should pass with no behavior change
5. Add `backend` field to Session and database
6. Install Codex CLI (`npm install -g @openai/codex`) and create `server/backends/codex.py` (direct subprocess, no third-party Python SDK)
7. Add backend selector to frontend

### Risks

- **Leaky abstraction**: The two backends have different item/event granularity. Claude Code emits per-block events (TextBlock, ToolUseBlock), while Codex emits per-item events (item.completed with various item types). The `BackendEvent` normalization in `_normalize_item()` needs careful mapping.
- **Tool approval divergence**: Claude Code has per-tool `can_use_tool` callbacks. Codex uses sandbox-level permissions with no per-tool approval. The `approve_tool()` method is a no-op for Codex.
- **Session resume semantics**: Claude Code uses opaque session IDs. Codex uses thread IDs persisted in `~/.codex/sessions/`. Both map to `backend_session_id` but the underlying mechanics differ.
- **Cost tracking**: Claude Code reports `total_cost_usd`. Codex reports token usage counts. We may need to normalize or display both formats.
- **No official Codex Python SDK**: We wrap the CLI directly via subprocess + JSONL. This is the same approach the official TypeScript SDK uses, so it's stable. But we own the JSONL parsing — if Codex changes its event format, we need to update.
- **Testing**: Need to test both backends independently. A mock backend implementing `BackendBase` would help for unit tests without requiring either CLI to be installed.

Sources:
- [Codex SDK docs](https://developers.openai.com/codex/sdk/)
- [openai-codex-sdk on PyPI](https://pypi.org/project/openai-codex-sdk/)
- [Codex SDK npm package](https://www.npmjs.com/package/@openai/codex-sdk)
- [Codex TypeScript SDK README](https://github.com/openai/codex/blob/main/sdk/typescript/README.md)
- [Proposal: Python SDK for Codex (Issue #5320)](https://github.com/openai/codex/issues/5320)

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
