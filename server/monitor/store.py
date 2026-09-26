"""The metrics store: a separate SQLite file, written without blocking a turn.

Separate from `octopus.db` deliberately (polish-2026-09.md §9 G2). That file is
already ~83 MB of product data with no retention policy, and metrics are
high-frequency and disposable: keeping them apart means they can be pruned on
their own schedule, deleted outright without touching session history, and a
defect in collection cannot corrupt anything a user cares about.

Two shapes, because two questions are asked of this data:

- `events` — one row per thing that happened, with dimensions
  (kind, session, agent, backend, error_code). This is what "which connector
  keeps failing" and "p95 turn latency by backend" are asked of, and both are
  SQL questions, which is why the store is SQL rather than a step-series.
- `samples` — one row per periodic gauge reading (memory, process counts, WAL
  size). Time and value, no dimensions beyond a label.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import aiosqlite

from ..aio import drain_cancelled

logger = logging.getLogger(__name__)

# The sink is bounded and lossy on purpose. A monitor that can stall a turn, or
# grow without limit when the writer falls behind, is worse than no monitor.
_QUEUE_MAX = 4096
_BATCH_MAX = 256
_WRITE_INTERVAL_SECONDS = 1.0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    ts          REAL NOT NULL,
    kind        TEXT NOT NULL,
    session_id  TEXT,
    agent_id    TEXT,
    backend     TEXT,
    duration_ms REAL,
    ok          INTEGER,
    error_code  TEXT,
    detail      TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_kind_ts ON events(kind, ts);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id, ts);

CREATE TABLE IF NOT EXISTS samples (
    ts     REAL NOT NULL,
    metric TEXT NOT NULL,
    value  REAL NOT NULL,
    labels TEXT
);
CREATE INDEX IF NOT EXISTS idx_samples_metric_ts ON samples(metric, ts);
"""


@dataclass
class Event:
    kind: str
    ts: float = field(default_factory=time.time)
    session_id: str | None = None
    agent_id: str | None = None
    backend: str | None = None
    duration_ms: float | None = None
    ok: bool | None = None
    error_code: str | None = None
    detail: dict[str, Any] | None = None


@dataclass
class Sample:
    metric: str
    value: float
    ts: float = field(default_factory=time.time)
    labels: dict[str, Any] | None = None


class MetricsStore:
    """Bounded in-memory queue in front of a batched SQLite writer.

    `record` must be callable from anywhere — inside a turn, inside an event
    parser, inside a middleware — so it does no I/O, takes no lock, and cannot
    raise into its caller. Under sustained backpressure it drops the OLDEST
    entries: recent data answers "what is happening", which is what this exists
    for, and a deque with a maxlen gives that for free.
    """

    def __init__(self, db_path: str, retention_days: int = 30) -> None:
        self._db_path = db_path
        self._retention_days = retention_days
        self._events: deque[Event] = deque(maxlen=_QUEUE_MAX)
        self._samples: deque[Sample] = deque(maxlen=_QUEUE_MAX)
        self._conn: aiosqlite.Connection | None = None
        self._writer: asyncio.Task | None = None
        self._stopping = False
        self.dropped = 0

    # ---------------------------------------------------------------- recording

    def record(self, event: Event) -> None:
        """Never blocks, never raises. Both matter: this is called from the turn
        path, and a metrics bug must not be able to fail a user's request."""
        try:
            if len(self._events) == self._events.maxlen:
                self.dropped += 1
            self._events.append(event)
        except Exception:  # pragma: no cover - defensive by design
            # The one place in the codebase where catching broadly is correct:
            # the alternative is letting telemetry break the thing it measures.
            logger.debug("metrics event dropped", exc_info=True)

    def sample(self, sample: Sample) -> None:
        try:
            if len(self._samples) == self._samples.maxlen:
                self.dropped += 1
            self._samples.append(sample)
        except Exception:  # pragma: no cover - defensive by design
            logger.debug("metrics sample dropped", exc_info=True)

    # ------------------------------------------------------------------ lifecycle

    async def initialize(self) -> None:
        self._conn = await aiosqlite.connect(self._db_path)
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA busy_timeout=5000")
        await self._conn.execute("PRAGMA synchronous=NORMAL")
        await self._conn.executescript(_SCHEMA)
        await self._conn.commit()

    def start(self) -> None:
        if self._writer is None:
            self._writer = asyncio.create_task(self._writer_loop())

    async def stop(self) -> None:
        self._stopping = True
        if self._writer:
            await drain_cancelled(self._writer, "monitor writer")
            self._writer = None
        await self.drain()
        if self._conn:
            await self._conn.close()
            self._conn = None

    async def _writer_loop(self) -> None:
        while not self._stopping:
            try:
                await asyncio.sleep(_WRITE_INTERVAL_SECONDS)
                await self.drain()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Same reasoning as `record`: a failed write costs data, never
                # the process.
                logger.warning("metrics writer iteration failed", exc_info=True)

    async def drain(self) -> int:
        """Flush what is queued. Returns rows written."""
        if self._conn is None:
            return 0
        events = [self._events.popleft() for _ in range(min(len(self._events), _BATCH_MAX))]
        samples = [self._samples.popleft() for _ in range(min(len(self._samples), _BATCH_MAX))]
        if not events and not samples:
            return 0
        try:
            if events:
                await self._conn.executemany(
                    "INSERT INTO events (ts, kind, session_id, agent_id, backend, "
                    "duration_ms, ok, error_code, detail) VALUES (?,?,?,?,?,?,?,?,?)",
                    [
                        (e.ts, e.kind, e.session_id, e.agent_id, e.backend,
                         e.duration_ms,
                         None if e.ok is None else int(e.ok),
                         e.error_code,
                         json.dumps(e.detail) if e.detail else None)
                        for e in events
                    ],
                )
            if samples:
                await self._conn.executemany(
                    "INSERT INTO samples (ts, metric, value, labels) VALUES (?,?,?,?)",
                    [
                        (s.ts, s.metric, s.value,
                         json.dumps(s.labels) if s.labels else None)
                        for s in samples
                    ],
                )
            await self._conn.commit()
        except Exception:
            logger.warning("metrics batch lost (%d events, %d samples)",
                           len(events), len(samples), exc_info=True)
            return 0
        return len(events) + len(samples)

    async def prune(self) -> int:
        """Drop anything past the retention window. Returns rows removed."""
        if self._conn is None:
            return 0
        cutoff = time.time() - self._retention_days * 86400
        removed = 0
        for table in ("events", "samples"):
            cur = await self._conn.execute(f"DELETE FROM {table} WHERE ts < ?", (cutoff,))
            removed += cur.rowcount or 0
        await self._conn.commit()
        if removed:
            logger.info("metrics pruned %d rows older than %d days",
                        removed, self._retention_days)
        return removed

    @property
    def conn(self) -> aiosqlite.Connection:
        assert self._conn is not None, "MetricsStore not initialized"
        return self._conn
