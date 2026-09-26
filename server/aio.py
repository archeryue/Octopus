"""Teardown helpers for asyncio tasks.

"Cancel a task, then await it so it finishes unwinding" appeared eight times
across the backend, each written as:

    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass

which is right about the cancellation and silent about everything else
(docs/plans/polish-2026-09.md §8 F4). The `CancelledError` is expected and says
nothing; any other exception is the task's last words — a stream decode that
blew up, a DB write that failed on the way out — and a teardown path is exactly
where those used to disappear. So: swallow the cancellation, log the rest, and
never raise, because every caller is releasing a process or a lock and must
finish doing it.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


async def drain_cancelled(task: asyncio.Task | None, what: str) -> None:
    """Cancel `task` if it is still running, then await it.

    `what` names the task in the log line, so a failure during shutdown can be
    attributed without a traceback-read.
    """
    if task is None:
        return
    if not task.done():
        task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass  # expected: we just cancelled it
    except Exception:
        logger.debug("%s raised while shutting down", what, exc_info=True)


async def stopped_within(
    coro: object, what: str, *, timeout: float | None = None
) -> None:
    """Await a best-effort stop, bounded and never raising.

    The second shape F4 found: `await asyncio.wait_for(x.stop(), timeout=…)`
    wrapped in `except (TimeoutError, Exception): pass`. A timeout is the point
    of the bound and needs no comment; anything else is the shutdown itself
    failing, which is worth a line. `timeout=None` awaits without a bound, for
    the call sites that had none — the change here is what gets logged, not how
    long anything waits.
    """
    try:
        if timeout is None:
            await coro  # type: ignore[misc]
        else:
            await asyncio.wait_for(coro, timeout=timeout)  # type: ignore[arg-type]
    except TimeoutError:
        logger.debug("%s did not stop within %.1fs", what, timeout)
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.debug("%s raised while stopping", what, exc_info=True)
