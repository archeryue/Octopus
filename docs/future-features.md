# Future Features

---

## 1. Handle Claude Code Interactive Input

**Priority**: Medium
**Affected**: `server/session_manager.py`, `web/src/components/ChatView.tsx`

### Context

Claude Code sometimes asks the user questions before proceeding — e.g., "Which approach should I take?" or "Pick one of these options:".

### SDK Investigation Result

The Claude Code SDK has no dedicated "ask human" message type. The `Message` union is:

```python
Message = UserMessage | AssistantMessage | SystemMessage | ResultMessage | StreamEvent
```

With `permission_mode: "bypassPermissions"`, Claude's questions are normal `AssistantMessage` text blocks, and the turn ends with a `ResultMessage`. The session returns to idle.

**The normal conversation flow already handles it:**

1. Claude asks a question → `AssistantMessage` (text) → `ResultMessage` → status idle
2. User sees the question in the chat UI / Telegram
3. User sends a new message with their answer
4. `send_message()` with `resume` continues the conversation

The only blocking input mechanism is tool approval via `SDKControlPermissionRequest`, which Octopus already handles through `_pending_approvals` and `approve_tool()`.

### UX Improvement (optional)

- **Visual indicator**: When Claude's last message ends with a question, show a "Claude is waiting for your response" hint.
- **Telegram**: Detect question patterns and avoid sending a "Session complete" indicator.

---

## 2. Multi-Backend Support (Claude Code + Codex)

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

## 3. Monitoring & Scheduled Tasks

**Priority**: High
**Affected**: new `server/scheduler.py`, `server/routers/schedules.py`, `web/`

### Context

Inspired by OpenClaw's 24/7 operation model — the ability to set up recurring tasks, cron-like schedules, and monitoring that runs while the user is away. Examples:
- Monitor GitHub issues and summarize new ones daily
- Run periodic health checks on services
- Schedule data processing or reporting tasks
- Watch for specific events and trigger session actions

### Possible Approach

- Use APScheduler or a lightweight cron-like scheduler within the server
- New API routes: create/list/delete scheduled tasks
- Each scheduled task triggers a `send_message()` to a designated session at the configured interval
- Frontend: schedule management UI (cron expression or simple interval picker)

---

## 4. Email Integration (Read & Write)

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
  - Scheduled email digests (combines with feature #3)

---

## 5. Long Session Performance (Message Virtualization)

**Priority**: High
**Affected**: `web/src/components/ChatView.tsx`, `server/routers/sessions.py`

### Problem

After extended use, a single session can accumulate hundreds or thousands of messages. The frontend renders all of them in the DOM at once, causing:

- **Browser freeze / jank**: The page becomes unresponsive as React renders thousands of message components.
- **High memory usage**: Each message with tool use output, code blocks, or diffs holds significant DOM state.
- **Slow session load**: `GET /api/sessions/{id}` returns all messages; the browser parses and renders the entire history on session switch.

This is especially bad for long-running sessions (feature #2 in long-running-plan.md makes this worse by keeping Claude running while the user is away — the session grows without anyone viewing it).

### Proposed Solutions

#### A. Virtualized scrolling (primary fix)

Only render messages visible in the viewport. Use a virtualization library like `@tanstack/react-virtual` or `react-virtuoso`:

```typescript
import { Virtuoso } from "react-virtuoso";

<Virtuoso
  data={messages}
  itemContent={(index, msg) => <MessageBubble message={msg} />}
  followOutput="smooth"   // auto-scroll on new messages
  initialTopMostItemIndex={messages.length - 1}
/>
```

This keeps DOM node count constant (~20-30 visible messages) regardless of total message count.

#### B. Server-side pagination

Add `?limit=N&offset=M` to `GET /api/sessions/{id}` (already planned in long-running-plan.md #1). Frontend loads the latest N messages on session switch, then fetches older messages on scroll-up:

```typescript
// Initial load: last 100 messages
const data = await fetch(`/api/sessions/${id}?limit=100`);

// On scroll to top: load previous batch
const older = await fetch(`/api/sessions/${id}?limit=100&offset=${currentOffset}`);
```

#### C. Collapsible message groups (UX improvement)

Group consecutive tool_use + tool_result messages into collapsible sections:

- **Collapsed view**: "Used 3 tools (Read, Edit, Bash)" with expand button
- **Expanded view**: Full tool details (current behavior)
- Auto-collapse tool groups older than the last 2 turns
- User can manually collapse/expand any group

#### D. Session summary / trim

For very long sessions (1000+ messages), offer a "Summarize & trim" action:

- Send a meta-prompt to Claude asking it to summarize the conversation so far
- Store the summary as a pinned message at the top
- Archive old messages (keep in DB but don't load by default)
- The `resume` session ID keeps Claude's full context — trimming the display doesn't affect Claude's memory

### Implementation Order

1. **Virtualized scrolling** — biggest bang for buck, no backend changes
2. **Server-side pagination** — already part of long-running-plan #1
3. **Collapsible tool groups** — UX polish
4. **Summary/trim** — only needed for extreme cases
