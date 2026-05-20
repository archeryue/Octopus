from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from apscheduler.schedulers.asyncio import AsyncIOScheduler

if TYPE_CHECKING:
    from server.database import Database
    from server.session_manager import SessionManager

logger = logging.getLogger(__name__)


class ScheduleRunner:
    def __init__(self, session_mgr: SessionManager, db: Database) -> None:
        self._scheduler = AsyncIOScheduler()
        self._session_mgr = session_mgr
        self._db = db

    async def initialize(self) -> None:
        for row in await self._db.load_schedules():
            if row["enabled"]:
                self._add_job(row)
        self._scheduler.start()

    def _add_job(self, row: dict) -> None:
        self._scheduler.add_job(
            self._fire,
            "interval",
            seconds=row["interval_seconds"],
            id=row["id"],
            args=[row["id"], row["agent_id"], row["prompt"]],
            replace_existing=True,
        )

    async def _fire(self, schedule_id: str, agent_id: str, prompt: str) -> None:
        """Materialize a fresh session under the agent, run the prompt, record
        the run, and hide the session on idle (agent-refactor.md §5.3/§5.6).
        Continuity across fires comes from agent memory (later), not a reused
        session."""
        session = None
        try:
            session = await self._session_mgr.create_session(
                agent_id, origin="schedule"
            )
            async for _event in self._session_mgr.send_message(session.id, prompt):
                pass
            now = datetime.now(timezone.utc).isoformat()
            await self._db.update_schedule(schedule_id, last_run_at=now)
        except ValueError as e:
            logger.info("Schedule %s skipped: %s", schedule_id, e)
        except Exception:
            logger.exception("Schedule %s failed", schedule_id)
        finally:
            if session is not None:
                await self._session_mgr.auto_archive_scheduled_session(session.id)

    async def add(self, row: dict) -> None:
        if row["enabled"]:
            self._add_job(row)

    async def remove(self, schedule_id: str) -> None:
        try:
            self._scheduler.remove_job(schedule_id)
        except Exception:
            pass

    async def reschedule(self, row: dict) -> None:
        await self.remove(row["id"])
        if row["enabled"]:
            self._add_job(row)

    async def shutdown(self) -> None:
        self._scheduler.shutdown(wait=False)
