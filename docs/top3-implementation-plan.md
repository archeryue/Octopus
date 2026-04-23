# Top 3 Implementation Plan

Scoped day-plan for the three highest-priority items from `future-features.md`. Each feature has been deliberately cut to fit a single focused day; deferred pieces are listed per-feature.

**Order to implement** (warm-up → biggest):
1. #3 Interactive input hint (frontend-only, ~45 min)
2. #1 Long session — virtualized scrolling (~2 hrs)
3. #2 Scheduled tasks MVP (~4–5 hrs backend + frontend)

---

## Feature #1 — Long Session Performance (virtualized scrolling only)

**Goal**: Replace `messages.map()` rendering in `ChatView` with `react-virtuoso` so DOM node count stays constant regardless of session length.

**Why virtuoso over tanstack-virtual**: Built-in `followOutput="smooth"` for auto-scroll-on-new-message and `initialTopMostItemIndex` for landing at bottom on mount — exactly what ChatView needs. No manual scroll code.

### Changes

**`web/package.json`** — add dep:
```
"react-virtuoso": "^4.x"
```
Install: `cd web && bun add react-virtuoso`

**`web/src/components/ChatView.tsx`** — replace `chat-messages` div with `<Virtuoso>`:
- Keep the `ToolApproval` vs `MessageBubble` branch inside `itemContent`.
- Drop the `bottomRef` scroll effect + `prevMsgCount` tracking — Virtuoso handles auto-scroll via `followOutput`.
- Loading dots: render as footer via `components={{ Footer: ... }}` when `isRunning`, so the virtualizer accounts for them in scroll math.
- Use `initialTopMostItemIndex={messages.length - 1}` on mount so session switch lands at the bottom.
- Default `followOutput="smooth"` behavior (only follow if already near bottom) means user scrolling up to read history won't be yanked down — keep it.

**`web/src/index.css`** — `.chat-messages` likely has `flex: 1; overflow-y: auto;`. Virtuoso needs a flex container with explicit height; change to `flex: 1 1 0; min-height: 0;` and pass `style={{ flex: 1 }}` to `<Virtuoso>`. Verify in browser.

### Tests

- Existing e2e tests should still pass — virtuoso renders visible items only; Playwright's auto-scroll helpers handle the rest. If an assertion relies on "all messages present in DOM", adjust to scroll into view first.
- No new unit tests needed — no logic changed.

### Scope cuts (deferred)

- Server-side pagination (`?limit/offset`)
- Collapsible tool groups
- Summary/trim

---

## Feature #2 — Monitoring & Scheduled Tasks (MVP)

**Goal**: "Every N minutes, send prompt P to session S." Interval-based only. CRUD UI.

### Dependency

**`pyproject.toml`** — add `apscheduler>=3.10`. APScheduler's `AsyncIOScheduler` integrates cleanly with FastAPI's lifespan.

### Database (`server/database.py`)

Add to `_SCHEMA`:
```sql
CREATE TABLE IF NOT EXISTS schedules (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    name TEXT NOT NULL,
    prompt TEXT NOT NULL,
    interval_seconds INTEGER NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    last_run_at TEXT,
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);
```

Add CRUD methods: `save_schedule`, `load_schedules`, `delete_schedule`, `update_schedule_last_run`, `set_schedule_enabled`. Mirror the patterns used by `save_session` / `load_sessions`.

### New module `server/scheduler.py`

```python
class ScheduleRunner:
    def __init__(self, session_mgr, db):
        self._scheduler = AsyncIOScheduler()
        self._session_mgr = session_mgr
        self._db = db
        self._jobs: dict[str, Job] = {}

    async def initialize(self):
        for row in await self._db.load_schedules():
            if row["enabled"]:
                self._schedule_one(row)
        self._scheduler.start()

    def _schedule_one(self, row):
        self._jobs[row["id"]] = self._scheduler.add_job(
            self._fire, "interval",
            seconds=row["interval_seconds"],
            id=row["id"],
            args=[row["id"], row["session_id"], row["prompt"]],
            replace_existing=True,
        )

    async def _fire(self, schedule_id, session_id, prompt):
        try:
            await self._session_mgr.start_message(session_id, prompt)
            await self._db.update_schedule_last_run(schedule_id, now_iso())
        except ValueError as e:
            # session busy or missing — skip this tick, log
            logger.info("Schedule %s skipped: %s", schedule_id, e)

    async def add(self, schedule_row): ...
    async def remove(self, schedule_id): ...
    async def set_enabled(self, schedule_id, enabled): ...
    async def shutdown(self): self._scheduler.shutdown(wait=False)
```

**Busy-session handling (MVP)**: If a fire hits a running session, `start_message` raises `ValueError`. The scheduler logs and skips — no retry, no queue. A real scheduler would queue or backoff; that's deferred.

### Router `server/routers/schedules.py`

New file, mirror `sessions.py` patterns:
- `GET /api/schedules` — list all
- `POST /api/schedules` — create `{session_id, name, prompt, interval_seconds}`
- `DELETE /api/schedules/{id}`
- `PATCH /api/schedules/{id}` — toggle `enabled` (also supports `name`, `prompt`, `interval_seconds` edits — triggers reschedule)

Validation: `interval_seconds >= 60` (prevent tight loops), prompt non-empty, session must exist.

### Main (`server/main.py`)

In `lifespan`:
```python
schedule_runner = ScheduleRunner(session_manager, db)
await schedule_runner.initialize()
app.state.schedule_runner = schedule_runner
# ... yield ...
await schedule_runner.shutdown()
```

Register router: `app.include_router(schedules.router)`.

### Pydantic models (`server/models.py`)

Add `ScheduleInfo`, `CreateScheduleRequest`, `UpdateScheduleRequest`.

### Frontend

**`web/src/stores/sessionStore.ts`** — add `schedules: Schedule[]` + `setSchedules`.

**New component `web/src/components/ScheduleList.tsx`**:
- Per active session: list schedules for that session, "New schedule" form (name, prompt textarea, interval in minutes).
- Each item: enable/disable toggle, delete button, `last_run_at` timestamp.
- All REST; no WS events in MVP.

**Integration**: Section in the sidebar under the active session (or small button on each session that opens a modal). Recommend: sidebar section — fewer new UI surfaces.

### Tests

- `tests/test_schedules.py`: CRUD endpoint tests (mirror `test_sessions.py`).
- `tests/test_scheduler.py`: unit test `_fire` behavior with a fake `session_mgr` — assert it calls `start_message`, handles the busy case.
- Skip e2e for scheduler in v1 (timing-based tests are flaky).

### Scope cuts (deferred)

- Cron expressions
- Retry / queueing on busy session
- Per-run history table
- Timezone configuration
- "Run now" button

---

## Feature #3 — Handle Interactive Input (visual indicator)

**Goal**: When Claude's last assistant message looks like a question, show a subtle "Claude is waiting for your response" hint above the input bar. Frontend-only.

### Changes

**`web/src/components/ChatView.tsx`**:

```typescript
const lastAssistantText = useMemo(() => {
  for (let i = messages.length - 1; i >= 0; i--) {
    const m = messages[i];
    if (m.role === "assistant" && m.type === "text") return m.content ?? "";
  }
  return "";
}, [messages]);

const isWaiting =
  !isRunning &&
  activeSession?.status === "idle" &&
  /\?\s*$/.test(lastAssistantText.trim());
```

Render between `chat-messages` and `chat-input-bar`:

```tsx
{isWaiting && (
  <div className="waiting-hint">Claude is waiting for your response</div>
)}
```

**`web/src/index.css`** — add `.waiting-hint` (muted color, small padding, italic).

### Detection rule

MVP: trailing `?` on the last assistant text block, and session is idle. Catches ~all natural questions without a classifier.

### Tests

- `web/src/components/ChatView.test.tsx` (new): render with a messages array ending in `"Which do you prefer?"`, assert hint appears. Render with `"Done."`, assert it doesn't.

### Scope cuts (deferred)

- Telegram-side detection
- Multi-language question patterns
- ML-based question classifier

---

## Execution Order & Budget

| Step | Feature | Est. |
|---|---|---|
| 1 | #3 (interactive hint) — smallest, warm up | 30–45 min |
| 2 | #1 (virtuoso) — install + swap + style fix + manual test | 1.5–2 hrs |
| 3 | #2 backend (DB + scheduler + router + tests) | 2.5–3 hrs |
| 4 | #2 frontend (list UI + create form) | 1.5–2 hrs |
| 5 | Full test suite sweep + commit + push | 30 min |

**Total**: ~6.5–8 hrs.

## Risks

- **Virtuoso + markdown-rendered messages with variable height**: Virtuoso measures dynamically — usually fine, but watch for initial layout jumps. If bad, set `increaseViewportBy={{ top: 400, bottom: 400 }}`.
- **APScheduler under uvicorn reload**: If `debug=True`, scheduler may start twice. Wrap `.start()` so it's a no-op if already running, or document "scheduler doesn't work in dev mode".
- **Scheduler on busy session**: MVP silently skips. Users won't see why a schedule didn't fire. Mitigation: log the skip, surface `last_run_at` in the UI so staleness is visible.
- **Test DB isolation**: `test_schedules.py` must use the same tmp-DB fixture as `test_sessions.py`.

## Verification (CLAUDE.md checklist)

All must pass before committing each feature:
- `.venv/bin/pytest tests/ -v` (currently 95; will grow with #2 tests)
- `cd web && bun run test` (currently 8; will grow with #3 test)
- `cd web && npx tsc --noEmit`
- `cd web && bun run test:e2e` (currently 17)
