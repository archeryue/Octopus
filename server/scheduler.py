from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

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
        now = datetime.now(timezone.utc)
        for row in await self._db.load_schedules():
            if row.get("run_at"):
                # One-time schedule: if the fire time is already past, it was
                # missed while the server was down. Delete it rather than
                # silently dropping or re-firing at startup.
                try:
                    run_date = datetime.fromisoformat(row["run_at"])
                    if run_date.tzinfo is None:
                        run_date = run_date.replace(tzinfo=timezone.utc)
                    if run_date <= now:
                        logger.info(
                            "Removing missed one-time schedule %s (was due %s)",
                            row["id"],
                            row["run_at"],
                        )
                        await self._db.delete_schedule(row["id"])
                        continue
                except ValueError:
                    await self._db.delete_schedule(row["id"])
                    continue
            if row["enabled"]:
                self._add_job(row)
        self._scheduler.start()

    def _add_job(self, row: dict) -> None:
        # Recurrence priority: run_at (one-time) > cron > interval.
        args = [
            row["id"],
            row["agent_id"],
            row["prompt"],
            row.get("origin_session_id"),
            row.get("run_at"),
        ]
        common = dict(id=row["id"], args=args, replace_existing=True)
        if row.get("run_at"):
            run_date = datetime.fromisoformat(row["run_at"])
            self._scheduler.add_job(self._fire, DateTrigger(run_date=run_date), **common)
        elif row.get("cron"):
            trigger = CronTrigger.from_crontab(
                row["cron"], timezone=ZoneInfo(row.get("timezone") or "UTC")
            )
            self._scheduler.add_job(self._fire, trigger, **common)
        else:
            self._scheduler.add_job(
                self._fire, "interval", seconds=row["interval_seconds"], **common
            )

    async def _fire(
        self,
        schedule_id: str,
        agent_id: str,
        prompt: str,
        origin_session_id: str | None = None,
        run_at: str | None = None,
    ) -> None:
        """Run the schedule's prompt for this fire.

        Two modes (agent-refactor.md §5.3/§5.6):

        * **Append into the origin session** — when the schedule was created from
          a `/schedule` chat command and that session is still live, the run is
          queued into it (`start_message`, so it lands behind any in-flight turn
          instead of being dropped) and the result shows up in the conversation
          the user already has open. The session is *not* archived.
        * **Fresh schedule-origin session** — no origin recorded, or it has since
          been deleted/archived. Materialize a throwaway session under the agent,
          run the prompt, and hide it on idle. Continuity across fires comes from
          agent memory, not a reused session.

        One-time schedules (`run_at` is set) are deleted from the DB after firing
        regardless of success — APScheduler already removed the DateTrigger job.
        """
        now = datetime.now(timezone.utc).isoformat()

        try:
            if origin_session_id and self._session_mgr.get_session(origin_session_id):
                try:
                    # Queue-aware: appends to the live session, waiting its turn if
                    # the user is mid-conversation rather than failing on a held lock.
                    await self._session_mgr.start_message(origin_session_id, prompt)
                    await self._db.update_schedule(schedule_id, last_run_at=now)
                except ValueError as e:
                    logger.info("Schedule %s skipped: %s", schedule_id, e)
                except Exception:
                    logger.exception("Schedule %s failed", schedule_id)
                return

            session = None
            try:
                session = await self._session_mgr.create_session(
                    agent_id, origin="schedule"
                )
                async for _event in self._session_mgr.send_message(session.id, prompt):
                    pass
                await self._db.update_schedule(schedule_id, last_run_at=now)
            except ValueError as e:
                logger.info("Schedule %s skipped: %s", schedule_id, e)
            except Exception:
                logger.exception("Schedule %s failed", schedule_id)
            finally:
                if session is not None:
                    await self._session_mgr.auto_archive_scheduled_session(session.id)
        finally:
            if run_at is not None:
                await self._db.delete_schedule(schedule_id)

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
