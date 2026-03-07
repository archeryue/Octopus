# Long-Running Stability — Implementation Plan

Make Octopus suitable for continuous operation over weeks/months.

---

## Dependency Graph

```
Phase 1 (independent, quick wins):
  #7  Disable Uvicorn Reload
  #5  Clean Up TextBuffer on Bridge Stop

Phase 2 (database layer):
  #4  Database Resilience
  #1  Lazy-Load Messages (depends on #4 — uses _ensure_connected)

Phase 3 (streaming architecture, biggest change):
  #3  Decouple Streaming from WebSocket (depends on #1 — no in-memory messages)
  #2  Sync on Reconnect (complements #3 — catches up after disconnect)

Phase 4 (bridge hardening):
  #6  Telegram Bridge Resilience
```

## Files Touched by Multiple Items

| File | Items | Coordination notes |
|---|---|---|
| `session_manager.py` | #1, #3 | #1 removes `messages` list, #3 changes task ownership + broadcast model. Both modify `Session` dataclass and `send_message()`. Do #1 first. |
| `database.py` | #1, #4 | #4 adds `_ensure_connected()` and batch commits. #1 adds `count_messages()` and pagination. Do #4 first so #1's new methods use `_ensure_connected()`. |
| `bridges/base.py` | #5, #6 | #5 adds buffer cleanup, #6 adds `healthy` property. No conflict — independent additions. |
| `main.py` | #6, #7 | #7 changes `reload` flag, #6 adds bridge health to `/health`. No conflict. |

## Broadcast Callback Refactor (folded into #3)

The old plan had a standalone item to change `_broadcast_callbacks` from a list to a dict. This is folded into #3 because #3 changes broadcasts to carry ALL events (not just status). The dict-keyed approach prevents duplicate callbacks on rapid WS reconnects.

---

## Phase 1 — Quick Wins

### #7: Disable Uvicorn Reload in Production

**Problem**: `uvicorn.run(..., reload=True)` is hardcoded on (line 104). In production, this watches the project directory for file changes and restarts the server — unnecessary overhead that can cause unexpected restarts when the DB file or logs change.

**Changes**:

`server/config.py` — add `debug` field:

```python
class Settings(BaseSettings):
    ...
    debug: bool = False
```

`server/main.py` — use it:

```python
uvicorn.run(
    "server.main:app",
    host=settings.host,
    port=settings.port,
    reload=settings.debug,
)
```

Developers set `OCTOPUS_DEBUG=true` in `.env` for dev mode.

---

### #5: Clean Up TextBuffer on Bridge Stop

**Problem**: When a bridge stops, pending flush tasks in `_text_buffers` are never cancelled. The `TextBuffer` objects and their asyncio tasks leak.

**Changes**:

`server/bridges/base.py` — add cleanup method and call it from `stop()`:

```python
async def _cleanup_buffers(self) -> None:
    """Cancel pending flush tasks and clear text buffers."""
    for buf in self._text_buffers.values():
        if buf._flush_task and not buf._flush_task.done():
            buf._flush_task.cancel()
    self._text_buffers.clear()
```

Option A — make the base class wrap `stop()`:

```python
async def shutdown(self) -> None:
    """Call this instead of stop() directly."""
    await self.stop()
    await self._cleanup_buffers()
```

Option B — call `_cleanup_buffers()` at the end of each subclass `stop()`. Option A is safer since it can't be forgotten.

Update `bridges/telegram.py` and `bridges/manager.py` to call `shutdown()` instead of `stop()`.

---

## Phase 2 — Database Layer

### #4: Database Resilience

**Problem**: Single `aiosqlite.Connection` with no reconnection logic. Every `append_message()` calls `commit()` immediately (line 142) — during a single Claude response that produces 20+ messages, that's 20+ fsyncs.

**Changes**:

#### A. Auto-reconnection

`server/database.py` — add `_ensure_connected()`, called at the top of every public method:

```python
async def _ensure_connected(self) -> None:
    if self._conn is None:
        logger.warning("Database connection lost, reconnecting...")
        self._conn = await aiosqlite.connect(self._db_path)
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
```

Add try/except around operations to set `self._conn = None` on failure, so the next call reconnects:

```python
async def append_message(self, ...) -> None:
    await self._ensure_connected()
    try:
        await self._conn.execute("INSERT INTO messages ...", ...)
    except Exception:
        self._conn = None
        raise
```

#### B. Batch commits

Remove the per-message `commit()` from `append_message()`. Add an explicit flush method:

```python
async def flush(self) -> None:
    """Commit pending writes."""
    await self._ensure_connected()
    await self._conn.commit()
```

**Who calls `flush()`**: `session_manager.py` calls `db.flush()` at the end of each `send_message()` turn — one commit per Claude response instead of N commits for N messages. Also call `flush()` in `load_messages()` to ensure reads see latest writes.

Keep immediate `commit()` in `save_session()` and `delete_session()` — these are infrequent and need to be durable immediately.

---

### #1: Lazy-Load Messages from Database

**Problem**: Every `Session` holds a full `messages: list[MessageContent]` in memory. On startup, `initialize()` loads ALL messages for ALL sessions from SQLite. After weeks of use with many sessions, this grows without bound and slows startup.

**Key insight**: Octopus's message list is purely for display — it's never sent to Claude (Claude manages its own context via `resume`). So we don't need it in memory at all.

**Changes**:

#### A. Session dataclass

Remove `messages`, add `_message_count`:

```python
@dataclass
class Session:
    id: str
    name: str
    working_dir: str
    status: SessionStatus = SessionStatus.idle
    created_at: str = field(default_factory=...)
    claude_session_id: str | None = None
    _message_count: int = field(default=0, repr=False)
    _client: ClaudeSDKClient | None = field(default=None, repr=False)
    _pending_approvals: dict[str, PendingApproval] = field(default_factory=dict, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
```

#### B. Database additions

`server/database.py`:

```python
async def count_messages(self, session_id: str) -> int:
    await self._ensure_connected()
    cursor = await self._conn.execute(
        "SELECT COUNT(*) FROM messages WHERE session_id = ?", (session_id,)
    )
    row = await cursor.fetchone()
    return row[0]

async def load_messages(
    self, session_id: str, limit: int = 0, offset: int = 0
) -> list[dict[str, Any]]:
    await self._ensure_connected()
    await self.flush()  # ensure pending writes are visible
    query = (
        "SELECT role, type, content, tool_name, tool_input, tool_use_id, "
        "is_error, session_id_ref, cost "
        "FROM messages WHERE session_id = ? ORDER BY seq"
    )
    params: list = [session_id]
    if limit > 0:
        query += " LIMIT ? OFFSET ?"
        params.extend([limit, offset])
    cursor = await self._conn.execute(query, params)
    ...
```

#### C. session_manager.py changes

**`initialize()`**: Load sessions without messages. Set `_message_count` from `db.count_messages()`:

```python
async def initialize(self, db: Database) -> None:
    self.db = db
    for row in await db.load_sessions():
        session = Session(id=row["id"], name=row["name"], ...)
        session._message_count = await db.count_messages(session.id)
        self.sessions[session.id] = session
```

**`_persist_message()`**: Use `_message_count` as seq, then increment. No more `session.messages.append()`:

```python
async def _persist_message(self, session: Session, msg: MessageContent) -> None:
    if not self.db:
        return
    seq = session._message_count
    session._message_count += 1
    await self.db.append_message(session_id=session.id, seq=seq, ...)
```

**`send_message()`**: Remove all `session.messages.append(msg)` calls (currently ~10 places). Just call `_persist_message()` and yield the event.

**`import_session()`**: Persist each message to DB, increment `_message_count`. Don't store in memory.

#### D. routers/sessions.py changes

**`list_sessions()`**: Use `session._message_count` for `message_count` field.

**`get_session()`**: Load messages from DB on demand:

```python
messages_raw = await session_manager.db.load_messages(s.id)
return SessionDetail(
    ...,
    message_count=s._message_count,
    messages=[MessageContent(**m) for m in messages_raw],
)
```

#### E. bridges/manager.py changes

`/current` command: Use `session._message_count` instead of `len(session.messages)`.

#### Risks

- **Latency**: `GET /api/sessions/{id}` now queries SQLite. With WAL mode and an indexed `(session_id, seq)` query, this is sub-millisecond for hundreds of messages.
- **Concurrent reads during writes**: SQLite WAL handles this — readers don't block writers.

---

## Phase 3 — Streaming Architecture

### #3: Decouple Streaming from WebSocket + Session Lock Timeout

**Problem**: Two intertwined issues:

1. **Task cancellation on disconnect**: `ws.py` cancels all streaming tasks in `finally` (line 100-103). When the user backgrounds the browser tab, the WS disconnects and the Claude subprocess is killed mid-work.
2. **Stuck lock**: `session._lock` is held for the entire `_run_claude()` call. If the subprocess hangs, the lock is held forever and the session is permanently stuck.

**Key insight**: The streaming task should be owned by the **Session**, not the WebSocket connection. Claude must keep running even if the browser disconnects. Events reach clients via broadcast callbacks — the WS handler just relays them.

#### A. Session dataclass (additional field)

```python
@dataclass
class Session:
    ...  # all fields from #1
    _active_task: asyncio.Task | None = field(default=None, repr=False)
```

#### B. Broadcast callback refactor

Change `_broadcast_callbacks` from a list to a dict keyed by connection ID. This prevents duplicate callbacks on rapid WS reconnects (folded from old standalone item):

```python
class SessionManager:
    def __init__(self) -> None:
        self._broadcast_callbacks: dict[str, Callable] = {}

    def on_broadcast(self, key: str, callback: Callable) -> None:
        self._broadcast_callbacks[key] = callback

    def remove_broadcast(self, key: str) -> None:
        self._broadcast_callbacks.pop(key, None)

    async def _broadcast(self, message: dict) -> None:
        for cb in list(self._broadcast_callbacks.values()):
            try:
                await cb(message)
            except Exception:
                logger.exception("Broadcast callback error")
```

#### C. Broadcast ALL events from send_message()

Currently `send_message()` only broadcasts status changes. Change it to broadcast **every** event (text, tool_use, tool_result, result, error). This is what allows the WS handler to become a thin relay — it doesn't iterate the generator anymore, it just receives broadcasts:

```python
# Inside send_message(), after yielding each event:
await self._broadcast(event)
```

At the end of `send_message()`, call `db.flush()` (#4's batch commit).

#### D. New methods on SessionManager

```python
async def start_message(self, session_id: str, prompt: str) -> None:
    """Kick off a message as a background task owned by the session."""
    session = self.sessions.get(session_id)
    if session is None:
        raise ValueError(f"Session {session_id} not found")
    if session._active_task and not session._active_task.done():
        raise ValueError(f"Session {session_id} is busy")

    session._active_task = asyncio.create_task(
        self._drive_message(session_id, prompt)
    )

async def _drive_message(self, session_id: str, prompt: str) -> None:
    """Consume the send_message generator as a background task."""
    try:
        async for event in self.send_message(session_id, prompt):
            pass  # send_message persists + broadcasts each event
    except Exception:
        logger.exception("Background task error for session %s", session_id)
```

#### E. Lock timeout

```python
# In send_message():
try:
    await asyncio.wait_for(session._lock.acquire(), timeout=5.0)
except asyncio.TimeoutError:
    raise ValueError(f"Session {session_id} is busy")
```

#### F. Force-reset endpoint

`POST /api/sessions/{id}/reset` — manual escape hatch for stuck sessions:

```python
async def reset_session(self, session_id: str) -> None:
    session = self.sessions.get(session_id)
    if session is None:
        raise ValueError(f"Session {session_id} not found")
    if session._active_task and not session._active_task.done():
        session._active_task.cancel()
    if session._client:
        session._client.disconnect()
        session._client = None
    if session._lock.locked():
        session._lock.release()
    session.status = SessionStatus.idle
    await self._broadcast({
        "type": "status", "session_id": session_id, "status": "idle"
    })
```

Add endpoint in `routers/sessions.py`:

```python
@router.post("/sessions/{session_id}/reset")
async def reset_session(session_id: str):
    await session_manager.reset_session(session_id)
    return {"status": "ok"}
```

#### G. ws.py changes

Remove `_stream_response()`. Remove `tasks` set. The handler becomes a thin relay:

```python
# Generate unique connection ID
conn_id = uuid.uuid4().hex

async def broadcast(message: dict):
    try:
        await ws.send_json(message)
    except Exception:
        pass

session_manager.on_broadcast(conn_id, broadcast)

try:
    while True:
        data = await ws.receive_json()
        msg_type = data.get("type")

        if msg_type == "send_message":
            session_id = data.get("session_id")
            content = data.get("content", "")
            try:
                await session_manager.start_message(session_id, content)
            except ValueError as e:
                await ws.send_json({"type": "error", ...})

        elif msg_type == "approve_tool":
            ...
finally:
    session_manager.remove_broadcast(conn_id)
    # NO task cancellation — Claude keeps running
```

#### H. Bridge manager alignment

The bridge manager currently iterates `send_message()` directly in `_stream_to_bridge()`. Change it to call `start_message()` instead. Events reach bridges via the broadcast callback (already registered via `register_broadcast()`).

Update `_handle_incoming()` to call `session_manager.start_message()`:

```python
async def _handle_incoming(self, platform: str, chat_id: str, text: str):
    session_id = self._get_or_create_mapping(platform, chat_id)
    try:
        await self.session_mgr.start_message(session_id, text)
    except ValueError as e:
        bridge = self._bridges.get(platform)
        if bridge:
            await bridge.send_text(chat_id, f"Error: {e}")
```

Remove `_stream_to_bridge()` and simplify `_active_streams` — the session's `_active_task` now owns the Claude subprocess. The bridge manager only needs to track chat→session mappings, not streaming tasks.

**Important behavior change**: Currently the bridge manager cancels old tasks when a new message arrives on the same chat. With session-owned tasks, the session rejects new messages while busy. The bridge should inform the user: "Session is busy, please wait."

#### Summary of file changes

| File | What changes |
|---|---|
| `session_manager.py` | Add `_active_task` to Session. Add `start_message()`, `_drive_message()`, `reset_session()`. Broadcast all events. Change `_broadcast_callbacks` to dict. Lock timeout. |
| `routers/ws.py` | Remove `_stream_response()`, `tasks` set. Use `start_message()`. Generate `conn_id`. No task cancellation in finally. |
| `routers/sessions.py` | Add `POST /sessions/{id}/reset` endpoint. |
| `bridges/manager.py` | Remove `_stream_to_bridge()`, `_active_streams`. Call `start_message()`. Handle "busy" response. Use stable key for broadcast registration. |

---

### #2: Sync State on WebSocket Reconnect

**Problem**: When the WS disconnects and reconnects (tab backgrounded, network blip), events emitted during the gap are lost from the frontend. After #3, this matters more — Claude keeps running during disconnect, so there will be missed messages.

**Changes**:

`web/src/hooks/useWebSocket.ts` — add re-fetch in `onopen`:

```typescript
ws.onopen = () => {
  getState().setConnected(true);
  if (reconnectTimer.current) clearTimeout(reconnectTimer.current);

  const { activeSessionId, token } = getState();
  if (token) {
    // Re-fetch session list (picks up status changes, new sessions)
    fetch(`${window.location.origin}/api/sessions`, {
      headers: { Authorization: `Bearer ${token}` },
    })
      .then((r) => (r.ok ? r.json() : null))
      .then((sessions) => sessions && getState().setSessions(sessions))
      .catch(() => {});

    // Re-fetch active session messages
    if (activeSessionId) {
      fetch(`${window.location.origin}/api/sessions/${activeSessionId}`, {
        headers: { Authorization: `Bearer ${token}` },
      })
        .then((r) => (r.ok ? r.json() : null))
        .then(
          (data) =>
            data && getState().setMessages(activeSessionId, data.messages)
        )
        .catch(() => {});
    }
  }
};
```

**Edge case — reconnect during active streaming**: The re-fetch returns messages persisted so far. New events arriving via the fresh WS broadcast are appended by `addMessage`. Since `setMessages` replaces the array and subsequent `addMessage` calls append, there's no duplication.

**No backend changes needed.** The REST endpoints already exist. #1 ensures `GET /api/sessions/{id}` loads from DB (not stale memory).

---

## Phase 4 — Bridge Hardening

### #6: Telegram Bridge Resilience

**Problem**: The poll loop retries with a flat 5-second sleep on all errors. If the Telegram API is down for hours, this generates rapid error logs with no backoff. There's also no way to check bridge health from the API.

**Changes**:

#### A. Exponential backoff

`server/bridges/telegram.py` — modify `_poll_loop()`:

```python
async def _poll_loop(self) -> None:
    backoff = 5
    while True:
        try:
            resp = await self._client.get(
                f"{self._api_url}/getUpdates",
                params={"offset": self._offset, "timeout": 30},
                timeout=35,
            )
            if resp.status_code != 200:
                logger.error("Telegram API returned %d", resp.status_code)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 300)
                continue

            data = resp.json()
            if not data.get("ok"):
                logger.error("Telegram API error: %s", data)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 300)
                continue

            backoff = 5  # reset on success
            for update in data.get("result", []):
                self._offset = update["update_id"] + 1
                asyncio.create_task(self._handle_update(update))

        except asyncio.CancelledError:
            raise
        except httpx.TimeoutException:
            continue  # normal long-poll timeout, no backoff
        except Exception:
            logger.exception("Telegram poll error")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 300)
```

#### B. Health tracking

`server/bridges/telegram.py`:

```python
class TelegramBridge(Bridge):
    def __init__(self, ...):
        ...
        self._last_poll_ok: float = 0.0

    # In _poll_loop, after successful response:
    self._last_poll_ok = time.time()
```

`server/bridges/base.py` — add `healthy` property:

```python
class Bridge(ABC):
    @property
    def healthy(self) -> bool:
        return True  # subclasses override
```

`server/bridges/telegram.py` — override:

```python
@property
def healthy(self) -> bool:
    if self._last_poll_ok == 0:
        return True  # hasn't polled yet, not unhealthy
    return (time.time() - self._last_poll_ok) < 120  # 2 min staleness
```

#### C. Health endpoint

`server/main.py` — extend `/health`:

```python
@app.get("/health")
async def health():
    bridges_health = {}
    if hasattr(app.state, "bridge_manager"):
        for name, bridge in app.state.bridge_manager._bridges.items():
            bridges_health[name] = {"healthy": bridge.healthy}
    return {"status": "ok", "bridges": bridges_health}
```

Store `bridge_manager` on `app.state` during startup (already accessible, just needs to be assigned).

---

## Final State Reference

After all phases are complete, here's what the key structures look like:

### Session dataclass

```python
@dataclass
class Session:
    id: str
    name: str
    working_dir: str
    status: SessionStatus = SessionStatus.idle
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    claude_session_id: str | None = None
    _message_count: int = field(default=0, repr=False)         # #1
    _active_task: asyncio.Task | None = field(default=None, repr=False)  # #3
    _client: ClaudeSDKClient | None = field(default=None, repr=False)
    _pending_approvals: dict[str, PendingApproval] = field(default_factory=dict, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    # REMOVED: messages: list[MessageContent]                  # #1
```

### Event flow (after #3)

```
User sends message (via WS or bridge)
  → ws.py / bridge_manager calls session_manager.start_message()
  → start_message() creates asyncio.Task owned by Session._active_task
  → Task runs _drive_message() → iterates send_message()
  → send_message() for each event:
      1. Persists to DB via _persist_message()  (#1)
      2. Yields event (consumed by _drive_message, discarded)
      3. Broadcasts event to all callbacks  (#3)
  → At end of turn: db.flush()  (#4)
  → WS broadcast callback → ws.send_json() to browser
  → Bridge broadcast callback → bridge.handle_event() to Telegram
  → Browser disconnects → task keeps running
  → Browser reconnects → re-fetches from DB  (#2)
```

### Settings (final)

```python
class Settings(BaseSettings):
    auth_token: str = "changeme"
    host: str = "0.0.0.0"
    port: int = 8000
    default_working_dir: str = "."
    cors_origins: list[str] = [...]
    db_path: str = "octopus.db"
    debug: bool = False                    # #7
    enable_tunnel: bool = False
    telegram_bot_token: str | None = None
    telegram_allowed_chat_ids: list[str] = []
    telegram_api_base_url: str = "https://api.telegram.org"
```
