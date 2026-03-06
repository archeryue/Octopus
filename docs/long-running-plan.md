# Long-Running Stability — Implementation Plans

Detailed implementation plans for issues in [future-features.md](future-features.md).

---

## #1: Lazy-Load Messages from Database

**Problem**: Every `Session` object holds a full `messages: list[MessageContent]` in memory. On startup, all messages for all sessions are loaded from SQLite. This grows without bound.

**Key insight**: Claude Code manages its own conversation context via the `resume` session ID and has built-in compaction. Octopus's message list is purely for display — it's never sent to Claude. So we don't need it in memory at all.

### Current usage of `session.messages`

| Usage | Location | What it does |
|---|---|---|
| `append(msg)` | `session_manager.py` (10 places) | Append during streaming, import, init |
| `s.messages` | `routers/sessions.py:61,78` | Return full list in `GET /api/sessions/{id}` |
| `len(s.messages)` | `routers/sessions.py:20,59,76`, `bridges/manager.py:264` | Message count for display |
| `len(session.messages) - 1` | `session_manager.py:160` | Compute `seq` for DB insert |

### Design

**Remove `messages` from the `Session` dataclass entirely.** Replace all reads with DB queries.

#### Session dataclass

```python
@dataclass
class Session:
    id: str
    name: str
    working_dir: str
    status: SessionStatus = SessionStatus.idle
    created_at: str = ...
    claude_session_id: str | None = None
    _message_count: int = field(default=0, repr=False)  # cached count
    _client: ClaudeSDKClient | None = ...
    _pending_approvals: dict[str, PendingApproval] = ...
    _lock: asyncio.Lock = ...
```

No `messages` list. Just a `_message_count` int for the count queries, incremented on append.

#### Database changes

Add a method to get message count without loading all rows:

```python
async def count_messages(self, session_id: str) -> int:
    cursor = await self.conn.execute(
        "SELECT COUNT(*) FROM messages WHERE session_id = ?", (session_id,)
    )
    row = await cursor.fetchone()
    return row[0]
```

Add pagination to `load_messages`:

```python
async def load_messages(
    self, session_id: str, limit: int = 0, offset: int = 0
) -> list[dict[str, Any]]:
    query = (
        "SELECT role, type, content, tool_name, tool_input, tool_use_id, "
        "is_error, session_id_ref, cost "
        "FROM messages WHERE session_id = ? ORDER BY seq"
    )
    params: list = [session_id]
    if limit > 0:
        query += " LIMIT ? OFFSET ?"
        params.extend([limit, offset])
    cursor = await self.conn.execute(query, params)
    ...
```

#### Changes by file

**`session_manager.py`**

- `initialize()`: Load sessions without messages. Set `_message_count` from `db.count_messages()`.
- `_persist_message()`: Use `_message_count` as `seq`, then increment it. No more `session.messages.append()`.
  ```python
  async def _persist_message(self, session: Session, msg: MessageContent) -> None:
      if not self.db:
          return
      seq = session._message_count
      session._message_count += 1
      await self.db.append_message(session_id=session.id, seq=seq, ...)
  ```
- `send_message()` / `_run_claude()`: Remove all `session.messages.append(msg)` calls. Just call `_persist_message()` and yield the event. The message goes to the DB and to the WebSocket/bridge — no in-memory copy needed.
- `import_session()`: Persist each message to DB, increment `_message_count`. Don't store in memory.

**`routers/sessions.py`**

- `list_sessions()`: Use `session._message_count` for `message_count`.
- `get_session()`: Load messages from DB:
  ```python
  messages = await session_manager.db.load_messages(s.id)
  return SessionDetail(..., message_count=s._message_count, messages=[MessageContent(**m) for m in messages])
  ```
- `import_session()`: Same — read back from DB after import.

**`bridges/manager.py`**

- `/current` command: Use `session._message_count` instead of `len(session.messages)`.

**REST API** (optional, can be added later)

- Add `?limit=N&offset=M` query params to `GET /api/sessions/{id}` for frontend pagination.

### What stays the same

- `_persist_message()` still writes to DB on every message (same as now).
- WebSocket streaming still yields events in real-time (unchanged).
- Claude Code conversation context still managed by `resume` (unchanged).
- The `GET /api/sessions/{id}` endpoint still returns messages — just from DB, not memory.

### Migration

No DB schema changes needed. The `messages` table is unchanged. This is purely a server-side memory optimization — remove the in-memory list, read from DB on demand.

### Risks

- **Latency**: `GET /api/sessions/{id}` now hits SQLite instead of reading from memory. SQLite with WAL mode is fast enough — a single indexed query on `(session_id, seq)` for a few hundred messages is sub-millisecond.
- **Concurrent writes during read**: A streaming response writes messages while the REST endpoint reads them. SQLite WAL mode handles this (readers don't block writers).

---

## #2: Sync State on WebSocket Reconnect

**Problem**: When the browser tab is backgrounded or the network blips, the WebSocket disconnects and reconnects after 3s. Events emitted during the gap are lost from the frontend view. The user sees a stale conversation until they manually re-select the session.

**Key insight**: All messages are persisted to SQLite. The fix is simply to re-fetch on reconnect. This also eliminates the need to persist `activeSessionId` to localStorage (old #3) — the session selection survives in Zustand across reconnects within the same page, and on full page reload the user re-selects anyway.

### Design

On WebSocket `onopen`, re-fetch both the session list and the active session's messages.

#### `useWebSocket.ts`

```typescript
ws.onopen = () => {
  getState().setConnected(true);
  if (reconnectTimer.current) clearTimeout(reconnectTimer.current);

  // Catch up after reconnect
  const { activeSessionId, token } = getState();
  if (token) {
    // Refresh session list (picks up status changes, new sessions from bridges)
    fetch(`${window.location.origin}/api/sessions`, {
      headers: { Authorization: `Bearer ${token}` },
    })
      .then((r) => r.ok ? r.json() : null)
      .then((sessions) => sessions && getState().setSessions(sessions))
      .catch(() => {});

    // Refresh active session messages
    if (activeSessionId) {
      fetch(`${window.location.origin}/api/sessions/${activeSessionId}`, {
        headers: { Authorization: `Bearer ${token}` },
      })
        .then((r) => r.ok ? r.json() : null)
        .then((data) => data && getState().setMessages(activeSessionId, data.messages))
        .catch(() => {});
    }
  }
};
```

#### Why not sequence numbers?

A sequence-number protocol (server tracks last-sent seq per client, replays missed events on reconnect) would be more efficient but adds significant complexity — server-side per-client state, replay buffers, seq tracking. The REST re-fetch approach is simple, correct, and fast enough (one SQLite query on reconnect).

#### Edge case: reconnect during active streaming

If Claude is mid-response when the WS reconnects, the re-fetch returns messages persisted so far. New events arriving via the fresh WS connection will be appended via `addMessage`. Since `setMessages` replaces the array and subsequent `addMessage` calls append, there's no duplication — the re-fetched messages are the baseline, and new WS events extend them.

### Changes

- `useWebSocket.ts`: Add re-fetch logic in `onopen`.
- No backend changes needed.

---

## #4: Decouple Streaming from WebSocket + Session Lock Timeout

**Problem**: Two intertwined issues:

1. **Task cancellation on disconnect**: `ws.py` cancels all streaming tasks in the `finally` block (line 102-103) when a WebSocket disconnects. Users frequently put the browser in the background to let Claude run — this kills the subprocess mid-work.
2. **Stuck lock**: `session._lock` is held for the entire `_run_claude()` call. If the subprocess hangs, the lock is held forever and the session is permanently stuck.

### Design

#### Part A: Move task ownership from WebSocket to Session

The core change: the streaming task should be owned by the **Session**, not the WebSocket connection. Claude must keep running even if the browser disconnects.

**Current flow** (broken):
```
WS receives "send_message"
  → creates asyncio.Task(_stream_response)
    → iterates send_message() generator
    → sends each event via ws.send_json()
  → WS disconnect → task.cancel() → subprocess killed
```

**New flow**:
```
WS receives "send_message"
  → session_manager.start_message(session_id, prompt)
    → creates asyncio.Task stored on Session._active_task
    → task iterates send_message() generator
    → events are broadcast to all listeners (WS, bridges)
    → events are persisted to DB
  → WS disconnect → task keeps running
  → WS reconnect → client fetches missed messages via API (#2)
```

**SessionManager changes**:

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
    """Consume the send_message generator. Runs as background task."""
    try:
        async for event in self.send_message(session_id, prompt):
            # send_message already persists + broadcasts each event
            pass
    except Exception:
        logger.exception("Background stream error for session %s", session_id)
```

**Broadcast all events**: Currently `send_message()` only broadcasts status changes. Change it to broadcast **every event** (text, tool_use, tool_result, etc.) so connected WS clients see real-time updates without needing a direct `ws.send_json()` call.

**ws.py changes**:

```python
# In send_message handler:
if msg_type == "send_message":
    session_id = data.get("session_id")
    content = data.get("content", "")
    if not session_id or not content:
        await ws.send_json({"type": "error", "message": "..."})
        continue
    try:
        await session_manager.start_message(session_id, content)
    except ValueError as e:
        await ws.send_json({"type": "error", "session_id": session_id, "message": str(e)})

# In finally block:
finally:
    session_manager.remove_broadcast(broadcast)
    # DO NOT cancel tasks — let Claude keep running
```

Remove `_stream_response()` entirely. Remove the `tasks` set. The WS handler becomes a thin input relay — events reach the client via the broadcast callback, which is already registered.

#### Part B: Session lock timeout + force reset

For genuinely stuck subprocesses (not normal long-running tasks):

**Track active task on Session**:
```python
@dataclass
class Session:
    ...
    _active_task: asyncio.Task | None = field(default=None, repr=False)
```

**Timeout on lock acquisition** in `send_message()`:
```python
try:
    await asyncio.wait_for(session._lock.acquire(), timeout=5.0)
except asyncio.TimeoutError:
    raise ValueError(f"Session {session_id} is busy (another message is in progress)")
```

**Force-reset endpoint** for stuck sessions:
```
POST /api/sessions/{id}/reset
```
This would:
1. Cancel `session._active_task` (if running).
2. Call `session._client.disconnect()` to kill the subprocess.
3. Release the lock (if held).
4. Set status to idle.
5. Broadcast status change.

This is a manual escape hatch — users trigger it from the UI when a session is visibly stuck.

#### Part C: Bridge manager alignment

The bridge manager's `_active_streams` already tracks tasks per chat. Apply the same principle: tasks are owned by the session, not the bridge. When a bridge stops, active Claude tasks should keep running.

### Changes

- `session_manager.py`:
  - Add `start_message()` and `_drive_message()` methods.
  - Broadcast all events from `send_message()`, not just status.
  - Add `_active_task` field to `Session`.
  - Timeout on lock acquisition.
  - Add `reset_session()` method for force-reset.
- `routers/ws.py`:
  - Remove `_stream_response()`, remove `tasks` set.
  - Call `session_manager.start_message()` instead of creating local task.
  - Remove task cancellation from `finally` block.
- `routers/sessions.py`: Add `POST /api/sessions/{id}/reset` endpoint.
- `bridges/manager.py`: Align task ownership with session-based model.

---

## #5: Database Resilience

**Problem**: Single `aiosqlite.Connection` with no reconnection logic. Per-message `commit()` causes unnecessary I/O.

### Design

#### Reconnection

Wrap the connection property with a health check:

```python
@property
async def conn(self) -> aiosqlite.Connection:
    if self._conn is None:
        await self._reconnect()
    return self._conn

async def _reconnect(self) -> None:
    logger.warning("Reconnecting to database...")
    self._conn = await aiosqlite.connect(self._db_path)
    await self._conn.execute("PRAGMA journal_mode=WAL")
    await self._conn.execute("PRAGMA foreign_keys=ON")
```

Since `conn` is currently a sync property used with `await self.conn.execute(...)`, changing it to async requires updating all call sites to `(await self.conn).execute(...)` or using a different pattern. Simpler approach: add a `_ensure_connected()` method called at the top of each DB method.

#### Batch commits

Replace per-message `commit()` with periodic commits. Add a dirty flag and flush on read or periodically:

```python
async def append_message(self, ...) -> None:
    await self.conn.execute("INSERT INTO messages ...", ...)
    self._dirty = True

async def _maybe_commit(self) -> None:
    if self._dirty:
        await self.conn.commit()
        self._dirty = False
```

Call `_maybe_commit()` in `load_messages()` (ensure reads see latest writes) and via a periodic task (e.g. every 1 second).

The simplest approach that works: commit at the end of each `send_message()` turn instead of per-message. This means one commit per Claude response instead of N commits for N messages.

### Changes

- `database.py`: Add `_ensure_connected()`, batch commit support.
- `session_manager.py`: Call a "flush" method after `_run_claude()` completes.

---

## #6: Clean Up TextBuffer on Bridge Stop

**Problem**: Pending flush tasks and `_text_buffers` dict are never cleaned up.

### Design

Add cleanup to `Bridge` base class:

```python
async def _cleanup_buffers(self) -> None:
    for buf in self._text_buffers.values():
        if buf._flush_task and not buf._flush_task.done():
            buf._flush_task.cancel()
    self._text_buffers.clear()
```

Call `_cleanup_buffers()` from `stop()` in each bridge subclass, or make it automatic by having the base class wrap `stop()`.

### Changes

- `bridges/base.py`: Add `_cleanup_buffers()`, call it from a base `stop()` wrapper.

---

## #7: Broadcast Callback Deduplication

**Problem**: `_broadcast_callbacks` is a plain list. Rapid reconnects can cause duplicate entries.

### Design

Use a dict keyed by a unique connection ID:

```python
class SessionManager:
    def __init__(self) -> None:
        self._broadcast_callbacks: dict[str, Callable] = {}

    def on_broadcast(self, key: str, callback):
        self._broadcast_callbacks[key] = callback

    def remove_broadcast(self, key: str):
        self._broadcast_callbacks.pop(key, None)

    async def _broadcast(self, message: dict):
        for cb in list(self._broadcast_callbacks.values()):
            try:
                await cb(message)
            except Exception:
                logger.exception("Broadcast callback error")
```

In `ws.py`, generate a unique ID per connection:

```python
conn_id = uuid.uuid4().hex
session_manager.on_broadcast(conn_id, broadcast)
# ... in finally:
session_manager.remove_broadcast(conn_id)
```

For the bridge manager, use `"bridge:<platform>"` as the key.

### Changes

- `session_manager.py`: Change `_broadcast_callbacks` from list to dict.
- `routers/ws.py`: Pass a connection ID.
- `bridges/manager.py`: Pass a stable key.

---

## #8: Session List Auto-Refresh

**Problem**: `fetchSessions()` only runs on component mount. Sessions created via bridges or other tabs don't appear.

### Design

This is largely solved by #2 — the session list is re-fetched on every WS reconnect. For real-time updates without reconnection, add server-side broadcast events:

Add new broadcast event types:

```python
# In session_manager.create_session():
await self._broadcast({"type": "session_created", "session": {...}})

# In session_manager.delete_session():
await self._broadcast({"type": "session_deleted", "session_id": session_id})
```

Handle in `useWebSocket.ts`:

```typescript
case "session_created":
  // Re-fetch full list to stay in sync
  fetch(...)
  break;
case "session_deleted":
  getState().setSessions(
    getState().sessions.filter(s => s.id !== data.session_id)
  );
  break;
```

### Changes

- `session_manager.py`: Broadcast on create/delete.
- `useWebSocket.ts`: Handle new event types.

---

## #9: Telegram Bridge Resilience

**Problem**: Flat 5s retry on poll errors. No health visibility.

### Design

#### Exponential backoff

```python
async def _poll_loop(self) -> None:
    backoff = 5
    while True:
        try:
            resp = await self._client.get(...)
            backoff = 5  # reset on success
            # ... process updates ...
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Telegram poll error")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 300)  # cap at 5 minutes
```

#### Health status

Add a `healthy` property to bridges, track last successful poll timestamp:

```python
class TelegramBridge(Bridge):
    _last_poll_ok: float = 0  # timestamp

    @property
    def healthy(self) -> bool:
        return (time.time() - self._last_poll_ok) < 120  # 2 min staleness
```

Expose in `/health`:

```python
@app.get("/health")
async def health():
    return {
        "status": "ok",
        "bridges": {
            name: {"healthy": b.healthy}
            for name, b in bridge_manager._bridges.items()
        }
    }
```

This requires passing `bridge_manager` to the health endpoint (e.g. via `app.state`).

### Changes

- `bridges/telegram.py`: Exponential backoff, `_last_poll_ok` tracking.
- `bridges/base.py`: Add `healthy` property (default `True`).
- `main.py`: Expose bridge health in `/health`.

---

## #10: Disable Uvicorn Reload in Production

**Problem**: `reload=True` always on, causes unnecessary file watching overhead.

### Design

Add a `debug` setting and use it:

```python
# config.py
class Settings(BaseSettings):
    ...
    debug: bool = False

# main.py
def run():
    uvicorn.run(
        "server.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
    )
```

Default to `False`. Developers set `OCTOPUS_DEBUG=true` in `.env` for dev mode.

### Changes

- `config.py`: Add `debug: bool = False`.
- `main.py`: Use `settings.debug` for reload flag.

---

## #11: Clean Up Frontend Message Store on Session Delete

**Problem**: `messages[sessionId]` stays in Zustand after session is deleted.

### Design

In `SessionList.tsx` `deleteSession()`, clear the messages:

```typescript
const deleteSession = async (id: string) => {
  await fetch(...);
  setSessions(sessions.filter((s) => s.id !== id));
  setMessages(id, []);
  if (activeSessionId === id) setActiveSessionId(null);
};
```

### Changes

- `components/SessionList.tsx`: Add `setMessages(id, [])` on delete.

---

## #12: Session Status Recovery After Crash

**Problem**: On startup, all sessions load as `status=idle` even if they were mid-response when the server crashed.

### Design

On startup, check if the last message in each session is a `result` or `error`. If not, the session likely crashed mid-response. Log a warning and optionally add a system message:

```python
async def initialize(self, db: Database) -> None:
    ...
    for session in self.sessions.values():
        last_msg = await db.get_last_message(session.id)
        if last_msg and last_msg["type"] not in ("result", "error", "text"):
            logger.warning(
                "Session %s may have been interrupted (last message type: %s)",
                session.id, last_msg["type"]
            )
            # Optionally persist a system message noting the interruption
```

Add `get_last_message()` to `Database`:

```python
async def get_last_message(self, session_id: str) -> dict | None:
    cursor = await self.conn.execute(
        "SELECT role, type FROM messages WHERE session_id = ? ORDER BY seq DESC LIMIT 1",
        (session_id,),
    )
    row = await cursor.fetchone()
    return {"role": row[0], "type": row[1]} if row else None
```

### Changes

- `database.py`: Add `get_last_message()`.
- `session_manager.py`: Check last message type on init, log warning.
