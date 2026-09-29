"""`/api/monitor/*` — the collected data, for the in-app page.

Read-only. Opens the metrics file per request rather than holding a connection:
these are human-paced queries against a file the writer already owns, and a
second long-lived handle on it buys nothing.

**The operator's**, not an account's (multi-tenancy.md §8). Everything here is
box-level: resident memory, sidecar counts, HTTP latency, and turn and error
rates across the whole install. None of it is scoped to a user because the
events carry no owner — and rather than invent a half-scoped view, the page is
what it has always been, an operations view, restricted to whoever operates the
box. A per-account usage view is a separate feature (§11).
"""

from __future__ import annotations

import os

import aiosqlite
from fastapi import APIRouter, HTTPException, Query, status

from ..config import settings
from ..deps import OperatorUser
from ..monitor import query

router = APIRouter(prefix="/api/monitor", tags=["monitor"])


async def _connect() -> aiosqlite.Connection:
    if not os.path.exists(settings.metrics_db_path):
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "No metrics yet — the store is created when the server starts.",
        )
    return await aiosqlite.connect(f"file:{settings.metrics_db_path}?mode=ro", uri=True)


@router.get("/overview")
async def overview(window: str = Query("24h"), _operator: OperatorUser = None):
    conn = await _connect()
    try:
        return await query.overview(conn, window)
    finally:
        await conn.close()


@router.get("/{report}")
async def report(
    report: str, window: str = Query("7d"), _operator: OperatorUser = None
):
    fn = query.REPORTS.get(report)
    if fn is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"Unknown report {report!r}. Available: {', '.join(sorted(query.REPORTS))}",
        )
    conn = await _connect()
    try:
        return {"report": report, "window": window, "rows": await fn(conn, window)}
    finally:
        await conn.close()
