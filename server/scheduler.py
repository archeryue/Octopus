from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from .models import SessionStatus

if TYPE_CHECKING:
    from server.database import Database
    from server.session_manager import SessionManager

logger = logging.getLogger(__name__)


class ScheduleRunner:
    def __init__(self, session_mgr: SessionManager, db: Database) -> None:
        self._scheduler = AsyncIOScheduler()
        self._session_mgr = session_mgr
        self._db = db

    def next_run_at(self, schedule_id: str) -> str | None:
        """ISO timestamp of the next fire, straight from APScheduler's own
        trigger state — the only place that knows it for a cron in an
        arbitrary timezone. None when the schedule is disabled (no job) or the
        scheduler hasn't started yet."""
        try:
            job = self._scheduler.get_job(schedule_id)
        except Exception:
            return None
        run_time = getattr(job, "next_run_time", None) if job else None
        return run_time.isoformat() if run_time else None

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

        A fire never starts while the previous one is still outstanding
        (§"overlap"): a schedule that runs every 5 minutes and takes 10 would
        otherwise stack runs on top of each other until the box gives out. The
        skip is logged and the schedule keeps its cadence — the next tick tries
        again.

        One-time schedules (`run_at` is set) are deleted from the DB after firing
        regardless of success — APScheduler already removed the DateTrigger job.
        """
        now = datetime.now(timezone.utc).isoformat()
        row = await self._schedule_row(schedule_id)
        body = self._compose_prompt(row, prompt)

        try:
            if origin_session_id and self._session_mgr.get_session(origin_session_id):
                if self._backlogged(origin_session_id):
                    logger.info(
                        "Schedule %s skipped: its session already has a fire "
                        "waiting to run",
                        schedule_id,
                    )
                    return
                try:
                    # Queue-aware: appends to the live session, waiting its turn if
                    # the user is mid-conversation rather than failing on a held lock.
                    await self._session_mgr.start_message(origin_session_id, body)
                    await self._db.update_schedule(
                        schedule_id,
                        last_run_at=now,
                        last_run_session_id=origin_session_id,
                    )
                except ValueError as e:
                    logger.info("Schedule %s skipped: %s", schedule_id, e)
                except Exception:
                    logger.exception("Schedule %s failed", schedule_id)
                return

            if self._previous_fire_running(row):
                logger.info(
                    "Schedule %s skipped: the previous run (session %s) is "
                    "still going",
                    schedule_id,
                    (row or {}).get("last_run_session_id"),
                )
                return

            session = None
            try:
                session = await self._session_mgr.create_session(
                    agent_id, origin="schedule"
                )
                # Recorded BEFORE the turn runs: the overlap guard on the next
                # tick has to be able to see a fire that is still in flight,
                # and a fire that crashes mid-turn still leaves a session the
                # user can open and read.
                await self._db.update_schedule(
                    schedule_id, last_run_session_id=session.id
                )
                async for _event in self._session_mgr.send_message(session.id, body):
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

    async def _schedule_row(self, schedule_id: str) -> dict | None:
        """The schedule's current row, or None if it was deleted between the
        job firing and this lookup."""
        try:
            rows = await self._db.load_schedules()
        except Exception:
            logger.exception("Schedule %s: could not read its row", schedule_id)
            return None
        return next((r for r in rows if r["id"] == schedule_id), None)

    @staticmethod
    def _compose_prompt(row: dict | None, prompt: str) -> str:
        """The prompt with a marker saying where it came from.

        Every other machine-injected turn in Octopus announces itself —
        `[bg-task-result]`, `[agent-reply:…]` — and a schedule's didn't, so an
        agent woken at 07:00 could not tell its own daily run from something
        the user had just typed. That matters for what it does next: a
        scheduled run reports, it doesn't ask a follow-up question of someone
        who isn't there.
        """
        if not row:
            return prompt
        from .schedule_ai import recurrence_label_for

        name = (row.get("name") or "").strip() or "Scheduled task"
        return (
            f"[scheduled:{name} — {recurrence_label_for(row)}]\n"
            f"This turn was started by a schedule, not by the user. Do the "
            f"work and report the result; nobody is waiting to answer "
            f"questions.\n\n{prompt}"
        )

    def _backlogged(self, session_id: str) -> bool:
        """True when a session already has something queued.

        A fire queued behind the user's in-flight turn is fine and by design;
        a *second* fire queued behind the first one is a pile-up.
        """
        session = self._session_mgr.get_session(session_id)
        return bool(session and session._pending_queue)

    def _previous_fire_running(self, row: dict | None) -> bool:
        """True while the session the last fire ran in is still working.

        Only meaningful for the throwaway-session mode: each fire gets its own
        session, so a busy one means the previous fire hasn't finished.
        """
        sid = (row or {}).get("last_run_session_id")
        if not sid:
            return False
        session = self._session_mgr.get_session(sid)
        if session is None:
            return False
        return session.status != SessionStatus.idle or bool(session._pending_queue)

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
