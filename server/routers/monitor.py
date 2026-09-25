"""`/api/monitor/*` — the collected data, for the in-app page.

Read-only. Opens the metrics file per request rather than holding a connection:
these are human-paced queries against a file the writer already owns, and a
second long-lived handle on it buys nothing.
"""

from __future__ import annotations

import os

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Query, status

from ..auth import verify_token
from ..config import settings
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
async def overview(window: str = Query("24h"), _: str = Depends(verify_token)):
    conn = await _connect()
    try:
        return await query.overview(conn, window)
    finally:
        await conn.close()


@router.get("/{report}")
async def report(
    report: str, window: str = Query("7d"), _: str = Depends(verify_token)
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
