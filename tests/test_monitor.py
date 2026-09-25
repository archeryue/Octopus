"""Monitoring (polish-2026-09.md §9).

The case this exists for is in the repo's own history: the Gmail connector was
unavailable for eleven consecutive days, a daily schedule failed on each of
them, and nothing counted it. So the tests that matter most are the ones that
prove a repeated failure becomes visible, and that the collector itself cannot
break the thing it measures.
"""

from __future__ import annotations

import time

import aiosqlite
import pytest

from server.monitor import Event, MetricsStore, Sample, query
from server.monitor.sampler import collect, collect_db


@pytest.fixture
async def store(tmp_path):
    s = MetricsStore(str(tmp_path / "m.db"), retention_days=30)
    await s.initialize()
    yield s
    await s.stop()


@pytest.fixture
async def conn(store):
    """A read connection over the same file, after draining."""
    await store.drain()
    c = await aiosqlite.connect(store._db_path)
    yield c
    await c.close()


class TestSinkCannotBreakTheApp:
    def test_recording_without_a_store_is_a_no_op(self):
        """Hooks sit in feature code that also runs in tests, in the CLI and in
        `octopus handoff`, none of which have a server. A hook must not care."""
        from server.monitor import record, sample, set_store

        set_store(None)
        record(Event(kind="turn"))   # must not raise
        sample(Sample("x", 1.0))

    async def test_record_never_blocks_or_raises(self, store):
        store.record(Event(kind="turn", duration_ms=5.0))
        assert len(store._events) == 1

    async def test_the_queue_is_bounded_and_drops_oldest(self, store):
        cap = store._events.maxlen
        for i in range(cap + 50):
            store.record(Event(kind="turn", detail={"i": i}))
        assert len(store._events) == cap
        assert store.dropped == 50
        # Newest survived: recent data is what answers "what is happening now".
        assert store._events[-1].detail == {"i": cap + 49}

    async def test_a_write_failure_costs_data_not_the_process(self, store, monkeypatch):
        store.record(Event(kind="turn"))

        async def boom(*a, **k):
            raise RuntimeError("disk gone")

        monkeypatch.setattr(store.conn, "executemany", boom)
        assert await store.drain() == 0   # reported as nothing written, no raise


class TestPersistence:
    async def test_events_round_trip(self, store, conn):
        store.record(Event(kind="turn", session_id="s1", backend="codex",
                           duration_ms=12.5, ok=True, detail={"cost": 0.02}))
        await store.drain()
        cur = await conn.execute(
            "SELECT kind, session_id, backend, duration_ms, ok FROM events")
        assert list(await cur.fetchall()) == [("turn", "s1", "codex", 12.5, 1)]

    async def test_samples_round_trip(self, store, conn):
        store.sample(Sample("mcp_sidecar_count", 0.0))
        await store.drain()
        cur = await conn.execute("SELECT metric, value FROM samples")
        assert list(await cur.fetchall()) == [("mcp_sidecar_count", 0.0)]

    async def test_retention_drops_only_what_is_old(self, store, conn):
        store.record(Event(kind="turn", ts=time.time() - 40 * 86400))
        store.record(Event(kind="turn", ts=time.time()))
        await store.drain()
        assert await store.prune() == 1
        cur = await conn.execute("SELECT COUNT(*) FROM events")
        assert (await cur.fetchone())[0] == 1


class TestTheGmailCase:
    """A connector that fails every day must be one obvious row."""

    async def test_a_persistently_failing_connector_is_visible(self, store, conn):
        for day in range(11):
            store.record(Event(
                kind="connector_call", ok=False, error_code="connector_unavailable",
                ts=time.time() - day * 86400,
                detail={"installation_id": "gmail_e255c1"},
            ))
        store.record(Event(kind="connector_call", ok=True,
                           detail={"installation_id": "github_bd57a9"}))
        await store.drain()

        rows = await query.connectors(conn, "30d")
        worst = rows[0]
        assert worst["installation"] == "gmail_e255c1"
        assert worst["n"] == 11
        assert (worst["ok_n"] or 0) == 0, "eleven calls, zero successes"

    async def test_errors_report_ranks_by_frequency(self, store, conn):
        for _ in range(5):
            store.record(Event(kind="connector_call", ok=False, error_code="boom"))
        store.record(Event(kind="turn", ok=False, error_code="auth"))
        await store.drain()
        rows = await query.errors(conn, "24h")
        assert rows[0]["error_code"] == "boom" and rows[0]["n"] == 5


class TestReports:
    async def test_turn_percentiles(self, store, conn):
        for ms in (10, 20, 30, 40, 1000):
            store.record(Event(kind="turn", backend="claude-code",
                               duration_ms=float(ms), ok=True))
        await store.drain()
        rows = await query.turns(conn, "24h")
        assert rows[0]["n"] == 5
        assert rows[0]["p50_ms"] == 30.0
        assert rows[0]["p99_ms"] == 1000.0, "the tail must not be averaged away"

    async def test_schedules_report_separates_fired_from_skipped(self, store, conn):
        store.record(Event(kind="schedule_fire", ok=True, detail={"schedule_id": "a"}))
        store.record(Event(kind="schedule_fire", ok=False,
                           error_code="overlap_skip", detail={"schedule_id": "a"}))
        await store.drain()
        rows = await query.schedules(conn, "24h")
        assert rows[0]["fired"] == 1 and rows[0]["skipped"] == 1

    async def test_http_is_grouped_by_route_template(self, store, conn):
        """Route template, never raw path — otherwise every session id mints a
        new dimension and the table is useless within a day."""
        for _ in range(3):
            store.record(Event(kind="http", duration_ms=5.0, ok=True,
                               detail={"route": "/api/sessions/{session_id}"}))
        await store.drain()
        rows = await query.http(conn, "24h")
        assert rows[0]["route"] == "/api/sessions/{session_id}"
        assert rows[0]["n"] == 3

    async def test_overview_survives_an_empty_store(self, store, conn):
        data = await query.overview(conn, "24h")
        assert data["counts"] == [] and data["turns"] == []

    @pytest.mark.parametrize("window,expected_hours", [("30m", 0.5), ("24h", 24), ("7d", 168)])
    def test_window_parsing(self, window, expected_hours):
        delta = time.time() - query._since(window)
        assert abs(delta - expected_hours * 3600) < 5

    def test_an_unparseable_window_defaults_to_a_day(self):
        assert abs((time.time() - query._since("banana")) - 86400) < 5


class TestSampler:
    def test_collect_reports_the_gauges_b1_is_judged_on(self):
        metrics = {s.metric for s in collect()}
        assert "mcp_sidecar_count" in metrics, "the number B1 drove to zero"
        assert "engine_process_count" in metrics
        assert "server_pss_mb" in metrics

    def test_collect_db_reads_size_and_wal(self, tmp_path):
        p = tmp_path / "x.db"
        p.write_bytes(b"0" * 3_000_000)          # 3 MB, so rounding to 2dp shows it
        (tmp_path / "x.db-wal").write_bytes(b"0" * 1_500_000)
        metrics = {s.metric: s.value for s in collect_db(str(p))}
        assert metrics["db_size_mb"] == pytest.approx(3.0, abs=0.01)
        assert metrics["db_wal_mb"] == pytest.approx(1.5, abs=0.01)

    def test_a_missing_db_file_is_silent(self, tmp_path):
        assert collect_db(str(tmp_path / "nope.db")) == []
