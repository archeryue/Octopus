"""NotifierManager — registry + dispatch for notifier targets.

Lifecycle: created at app startup, `set_db(db)` once the database is
ready, `load()` to read all enabled targets, then call
`fire(event)` from triggers (currently only session-idle in
session_manager) to dispatch in parallel.

Adding a new notifier type: add a class to `_make`'s dispatch.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from .base import NotifierBase, NotifierEvent
from .webhook import WebhookNotifier

if TYPE_CHECKING:
    from ..database import Database

logger = logging.getLogger(__name__)


class NotifierManager:
    def __init__(self) -> None:
        self._db: Database | None = None
        self._notifiers: dict[str, NotifierBase] = {}

    def set_db(self, db: Database) -> None:
        self._db = db

    async def load(self) -> None:
        """Re-read all notifiers from DB. Call after CRUD changes."""
        if self._db is None:
            return
        rows = await self._db.load_notifiers()
        self._notifiers = {}
        for row in rows:
            if not row.get("enabled"):
                continue
            notifier = self._make(row)
            if notifier is not None:
                self._notifiers[notifier.id] = notifier

    def list(self) -> list[NotifierBase]:
        return list(self._notifiers.values())

    def _make(self, row: dict[str, Any]) -> NotifierBase | None:
        """Build a concrete NotifierBase from a DB row, by type."""
        t = row["type"]
        if t == "webhook":
            return WebhookNotifier(
                id=row["id"], label=row["label"], config=row["config"]
            )
        logger.warning("Unknown notifier type %s (id=%s); skipping", t, row["id"])
        return None

    async def fire(self, event: NotifierEvent) -> None:
        """Dispatch `event` to every registered notifier in parallel.

        Notifier exceptions are caught + logged so a single bad target
        doesn't poison the rest.
        """
        if not self._notifiers:
            return
        targets = list(self._notifiers.values())
        logger.debug(
            "Firing %s to %d notifier(s)", event.type, len(targets)
        )
        await asyncio.gather(
            *(self._safe_send(n, event) for n in targets),
            return_exceptions=False,
        )

    @staticmethod
    async def _safe_send(notifier: NotifierBase, event: NotifierEvent) -> None:
        try:
            await notifier.send(event)
        except Exception:
            logger.exception(
                "Notifier %s (%s) raised on send", notifier.id, notifier.type
            )


# App-lifetime singleton — wired into the FastAPI lifespan in main.py.
notifier_manager = NotifierManager()
