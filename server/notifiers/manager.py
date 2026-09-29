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
        # Which account each target belongs to (multi-tenancy.md §7). A
        # notifier is somebody's webhook, and `fire` used to send every event to
        # all of them — so one person's session going idle would have poked
        # everybody else's endpoint.
        self._owner: dict[str, str | None] = {}

    def set_db(self, db: Database) -> None:
        self._db = db

    async def load(self) -> None:
        """Re-read all notifiers from DB. Call after CRUD changes."""
        if self._db is None:
            return
        rows = await self._db.load_notifiers()
        self._notifiers = {}
        self._owner = {}
        for row in rows:
            if not row.get("enabled"):
                continue
            notifier = self._make(row)
            if notifier is not None:
                self._notifiers[notifier.id] = notifier
                self._owner[notifier.id] = row.get("user_id")

    def list(self, user_id: str | None = None) -> list[NotifierBase]:
        return [
            n
            for n in self._notifiers.values()
            if user_id is None or self._owner.get(n.id) == user_id
        ]

    def _make(self, row: dict[str, Any]) -> NotifierBase | None:
        """Build a concrete NotifierBase from a DB row, by type."""
        t = row["type"]
        if t == "webhook":
            return WebhookNotifier(
                id=row["id"], label=row["label"], config=row["config"]
            )
        logger.warning("Unknown notifier type %s (id=%s); skipping", t, row["id"])
        return None

    async def fire(self, event: NotifierEvent, user_id: str | None = None) -> None:
        """Dispatch `event` to the account's registered notifiers, in parallel.

        `user_id=None` is the pre-accounts install, where every target is the
        one operator's. With accounts it is required in practice: an event
        carries somebody's session name and the fact that they are working, and
        a webhook is an address off this box.

        Notifier exceptions are caught + logged so a single bad target
        doesn't poison the rest.
        """
        targets = self.list(user_id)
        if not targets:
            return
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
