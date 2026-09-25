"""Canned questions, because collection without analysis is just disk use.

Every function here returns plain rows. The CLI formats them, the API serves
them, and `sqlite3 octopus-metrics.db` answers anything not anticipated — which
is the reason the store is dimensional SQL rather than a step-series.
"""

from __future__ import annotations

import time
from typing import Any

import aiosqlite


def _since(window: str) -> float:
    """`7d` / `24h` / `30m` -> a unix timestamp. Anything unparseable is 24h,
    because a mistyped window should show something rather than nothing."""
    units = {"m": 60, "h": 3600, "d": 86400}
    try:
        n, unit = int(window[:-1]), window[-1]
        return time.time() - n * units[unit]
    except (ValueError, KeyError, IndexError):
        return time.time() - 86400


async def _rows(conn: aiosqlite.Connection, sql: str, params: tuple) -> list[dict[str, Any]]:
    cur = await conn.execute(sql, params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r, strict=True)) for r in await cur.fetchall()]


def _percentile(sorted_vals: list[float], p: float) -> float | None:
    """Nearest-rank percentile of an already-sorted list. A free function rather
    than a closure over the loop variable: ruff's B023 is right that capturing
    it is fragile even where, as here, the call is immediate."""
    if not sorted_vals:
        return None
    return round(sorted_vals[min(int(len(sorted_vals) * p), len(sorted_vals) - 1)], 1)


async def turns(conn: aiosqlite.Connection, window: str = "7d") -> list[dict[str, Any]]:
    """Turn latency and cost by backend. Percentiles come from ordering rather
    than a window function so this works on any SQLite build."""
    since = _since(window)
    out = []
    backends = await _rows(
        conn,
        "SELECT backend, COUNT(*) n, SUM(ok) ok_n, "
        "       AVG(duration_ms) avg_ms, MAX(duration_ms) max_ms "
        "FROM events WHERE kind='turn' AND ts >= ? GROUP BY backend",
        (since,),
    )
    for b in backends:
        durs = await _rows(
            conn,
            "SELECT duration_ms FROM events WHERE kind='turn' AND ts >= ? "
            "AND backend IS ? AND duration_ms IS NOT NULL ORDER BY duration_ms",
            (since, b["backend"]),
        )
        vals = [d["duration_ms"] for d in durs]
        b.update(
            p50_ms=_percentile(vals, 0.50),
            p95_ms=_percentile(vals, 0.95),
            p99_ms=_percentile(vals, 0.99),
        )
        out.append(b)
    return out


async def errors(conn: aiosqlite.Connection, window: str = "7d") -> list[dict[str, Any]]:
    """What is failing, most often first."""
    return await _rows(
        conn,
        "SELECT kind, error_code, COUNT(*) n, MAX(ts) last_seen "
        "FROM events WHERE ts >= ? AND (ok = 0 OR error_code IS NOT NULL) "
        "GROUP BY kind, error_code ORDER BY n DESC",
        (_since(window),),
    )


async def connectors(conn: aiosqlite.Connection, window: str = "7d") -> list[dict[str, Any]]:
    """Per-connector success rate. The Gmail outage would have been one row here
    with ok_n = 0 and a growing n (§9 G0)."""
    return await _rows(
        conn,
        "SELECT json_extract(detail, '$.installation_id') installation, "
        "       COUNT(*) n, SUM(ok) ok_n, MAX(ts) last_seen "
        "FROM events WHERE kind='connector_call' AND ts >= ? "
        "GROUP BY installation ORDER BY (COUNT(*) - IFNULL(SUM(ok),0)) DESC",
        (_since(window),),
    )


async def schedules(conn: aiosqlite.Connection, window: str = "7d") -> list[dict[str, Any]]:
    return await _rows(
        conn,
        "SELECT json_extract(detail, '$.schedule_id') schedule, "
        "       SUM(ok) fired, SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END) skipped, "
        "       MAX(ts) last_seen "
        "FROM events WHERE kind='schedule_fire' AND ts >= ? "
        "GROUP BY schedule ORDER BY skipped DESC",
        (_since(window),),
    )


async def resources(conn: aiosqlite.Connection, window: str = "24h") -> list[dict[str, Any]]:
    """Latest and peak for each gauge. `mcp_sidecar_count` is the one to watch:
    it should be 0 now that the namespaces are served in-process (§4 B1)."""
    return await _rows(
        conn,
        "SELECT metric, COUNT(*) n, "
        "       ROUND(AVG(value), 2) avg, ROUND(MAX(value), 2) peak, "
        "       (SELECT ROUND(value, 2) FROM samples s2 "
        "        WHERE s2.metric = samples.metric ORDER BY ts DESC LIMIT 1) latest "
        "FROM samples WHERE ts >= ? GROUP BY metric ORDER BY metric",
        (_since(window),),
    )


async def http(conn: aiosqlite.Connection, window: str = "24h") -> list[dict[str, Any]]:
    return await _rows(
        conn,
        "SELECT json_extract(detail, '$.route') route, COUNT(*) n, "
        "       ROUND(AVG(duration_ms), 1) avg_ms, ROUND(MAX(duration_ms), 1) max_ms "
        "FROM events WHERE kind='http' AND ts >= ? "
        "GROUP BY route ORDER BY n DESC LIMIT 25",
        (_since(window),),
    )


async def overview(conn: aiosqlite.Connection, window: str = "24h") -> dict[str, Any]:
    """Everything at a glance — what the in-app page renders."""
    counts = await _rows(
        conn,
        "SELECT kind, COUNT(*) n, SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END) failures "
        "FROM events WHERE ts >= ? GROUP BY kind ORDER BY n DESC",
        (_since(window),),
    )
    return {
        "window": window,
        "counts": counts,
        "turns": await turns(conn, window),
        "errors": (await errors(conn, window))[:10],
        "resources": await resources(conn, window),
    }


REPORTS = {
    "turns": turns,
    "errors": errors,
    "connectors": connectors,
    "schedules": schedules,
    "resources": resources,
    "http": http,
}
