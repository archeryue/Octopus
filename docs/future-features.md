# Future Features — Long-Running Stability

Issues and improvements to make Octopus suitable for continuous, long-running operation (days/weeks).

---

## 1. Cap In-Memory Message History ✅ Planned

**Priority**: Critical · **Plan**: [long-running-plan.md #1](long-running-plan.md)
**Affected**: `server/session_manager.py`

Lazy-load messages from DB on demand. Remove `messages` list from Session dataclass, replace with `_message_count` int. Load via `load_messages()` with pagination.

---

## 2. Sync Messages on WebSocket Reconnect ✅ Planned

**Priority**: Critical · **Plan**: [long-running-plan.md #2](long-running-plan.md)
**Affected**: `web/src/hooks/useWebSocket.ts`, `web/src/components/SessionList.tsx`

Re-fetch active session messages and session list in `onopen` on reconnect.

---

## ~~3. Persist Active Session ID~~ — Removed

Unnecessary. #2 (sync on reconnect) handles the real problem — the session stays selected in Zustand across reconnects within the same page. On full page reload, re-selecting is acceptable UX.

---

## 4. Decouple Streaming from WebSocket + Session Lock Timeout ✅ Planned

**Priority**: High · **Plan**: [long-running-plan.md #4](long-running-plan.md)
**Affected**: `server/session_manager.py`, `server/routers/ws.py`

Move task ownership from WS to Session so Claude keeps running when browser is backgrounded. Add lock timeout and force-reset endpoint for stuck subprocesses.

---

## 5. Database Resilience ✅ Planned

**Priority**: High · **Plan**: [long-running-plan.md #5](long-running-plan.md)
**Affected**: `server/database.py`

Add `_ensure_connected()` for auto-reconnection. Batch commits per Claude response instead of per message.

---

## 6. Clean Up TextBuffer on Bridge Stop ✅ Planned

**Priority**: Medium · **Plan**: [long-running-plan.md #6](long-running-plan.md)
**Affected**: `server/bridges/base.py`

Add `_cleanup_buffers()` to cancel pending flush tasks and clear `_text_buffers` on bridge stop.

---

## 7. Broadcast Callback Deduplication

**Priority**: Medium
**Affected**: `server/session_manager.py`, `server/routers/ws.py`

### Problem

`_broadcast_callbacks` is a plain list. Each WebSocket connection appends a closure. On rapid reconnects, there's a window where the old callback isn't removed before the new one is added, causing duplicate broadcasts to the same client.

### Fix

Use a dict keyed by connection ID instead of a list:

```python
self._broadcast_callbacks: dict[str, Callable] = {}

def on_broadcast(self, key: str, callback):
    self._broadcast_callbacks[key] = callback

def remove_broadcast(self, key: str):
    self._broadcast_callbacks.pop(key, None)
```

---

## 8. Session List Auto-Refresh

**Priority**: Medium
**Affected**: `web/src/components/SessionList.tsx`

### Problem

`fetchSessions()` only runs on component mount. If a session is created via the Telegram bridge, CLI handoff, or another browser tab, the sidebar doesn't update until page reload.

### Fix

- Re-fetch sessions on WebSocket reconnect.
- Add a `session_created` / `session_deleted` broadcast event from the server, and update the sessions list in the WebSocket message handler.
- Alternatively, poll sessions every 30 seconds as a simpler fallback.

---

## 9. Telegram Bridge Resilience

**Priority**: Medium
**Affected**: `server/bridges/telegram.py`

### Problem

The poll loop retries with a flat 5-second sleep on errors, with no exponential backoff. If the Telegram API is down for an extended period, this generates rapid error logs. There's also no way to check bridge health from the web UI or API — the bridge could be silently failing.

### Fix

- Add exponential backoff (5s, 10s, 30s, 60s, capped at 5 minutes).
- Add a bridge health status to the `/health` endpoint (e.g. `{"status": "ok", "bridges": {"telegram": "connected"}}`).
- Track last successful poll timestamp for staleness detection.

---

## 10. Disable Uvicorn Reload in Production

**Priority**: Medium
**Affected**: `server/main.py:104`

### Problem

`uvicorn.run(..., reload=True)` is always on. In production, this watches the entire project directory for file changes and restarts the server — unnecessary overhead, and it can cause unexpected restarts if logs or the DB file change.

### Fix

Make `reload` configurable or default to `False`:

```python
def run():
    uvicorn.run(
        "server.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug,  # only reload in dev
    )
```

---

## 11. Clean Up Frontend Message Store on Session Delete

**Priority**: Low
**Affected**: `web/src/stores/sessionStore.ts`, `web/src/components/SessionList.tsx`

### Problem

When a session is deleted, `setSessions` filters it from the list but `messages[sessionId]` remains in the Zustand store forever. Over many create/delete cycles, this accumulates stale data.

### Fix

Clear messages for the deleted session:

```typescript
const deleteSession = async (id: string) => {
  // ... existing delete logic ...
  setMessages(id, []);  // or delete the key
};
```

---

## 12. Session Status Recovery After Crash

**Priority**: Low
**Affected**: `server/session_manager.py`

### Problem

On startup, all sessions are loaded with `status=idle` (the dataclass default). If the server crashed while a session was running, there's no indication to the user. The `claude_session_id` is valid so `resume` will work, but the session appears as if nothing happened.

### Fix

- Log a warning for sessions that had an active `claude_session_id` but no result message as their last entry — they likely crashed mid-response.
- Optionally, add a `last_activity` timestamp to sessions and show a "session may have been interrupted" indicator in the UI.
