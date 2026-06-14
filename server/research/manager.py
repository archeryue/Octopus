"""ResearchManager — tracks deep-research jobs as async tasks
(native-deep-research.md §6). Mirrors the bg-task / delegation managers:

  - `start(session_id, question)` persists a `research_jobs` row, returns the
    id immediately, and runs the pipeline as a tracked `asyncio.Task`.
  - Progress is broadcast over the session bus + written to the row's `phase`.
  - On success the report is written to a file AND injected into the session as
    a turn via `start_message` (the bg-delivery path); completion and delivery
    are tracked separately, injection is idempotent.
  - `cancel(job_id)` cancels the task — leaves re-raise CancelledError and reap
    their process groups, so nothing orphans.
  - A global semaphore bounds concurrent JOBS (per-job leaf concurrency is
    bounded inside the pipeline); a hard per-job timeout backstops everything.
  - A boot sweep marks restart-orphaned `running` rows interrupted.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from ..config import settings
from ..harness import get_harness, has_backend
from .orchestrator import ResearchLimits, ResearchProgress, run_research

if TYPE_CHECKING:
    from ..database import Database
    from ..session_manager import SessionManager

logger = logging.getLogger(__name__)


class ResearchError(Exception):
    """Surface-level error with an HTTP status for the REST layer."""

    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _research_dir() -> str:
    d = os.path.expanduser(os.path.join("~", ".octopus", "research"))
    os.makedirs(d, exist_ok=True)
    return d


class ResearchManager:
    """App-lifetime singleton; bound in main.py's lifespan."""

    def __init__(self) -> None:
        self.session_mgr: "SessionManager | None" = None
        self.db: "Database | None" = None
        self._tasks: dict[str, asyncio.Task] = {}
        self._job_sem: asyncio.Semaphore | None = None

    def bind(self, session_mgr: "SessionManager", db: "Database") -> None:
        self.session_mgr = session_mgr
        self.db = db
        self._job_sem = asyncio.Semaphore(max(1, settings.research_max_concurrent_jobs))

    async def recover_interrupted(self) -> int:
        """Boot sweep — mark prior-process `running` jobs interrupted."""
        if self.db is None:
            return 0
        n = await self.db.mark_in_flight_research_jobs_interrupted(_now())
        if n:
            logger.info("research: marked %d interrupted job(s) on boot", n)
        return n

    # ----------------------------------------------------------------- start

    async def start(self, session_id: str, question: str) -> dict[str, Any]:
        """Create + launch a research job for `session_id`. Returns the row."""
        if self.session_mgr is None or self.db is None:
            raise ResearchError("ResearchManager not bound", status_code=500)
        question = (question or "").strip()
        if not question:
            raise ResearchError("question must be a non-empty string", status_code=400)

        session = self.session_mgr.get_session(session_id)
        if session is None:
            raise ResearchError(f"session {session_id} not found", status_code=404)
        if not has_backend(session.backend):
            raise ResearchError(f"unknown backend {session.backend!r}", status_code=400)
        harness = get_harness(session.backend)
        if harness.profile.web is None:
            raise ResearchError(
                f"the {session.backend} backend has no web tools, so deep "
                "research isn't available on it",
                status_code=409,
            )

        job_id = uuid.uuid4().hex[:12]
        await self.db.create_research_job(job_id, session_id, question, _now())
        await self._broadcast({
            "type": "research_started",
            "session_id": session_id,
            "research_id": job_id,
            "question": question,
        })
        task = asyncio.create_task(self._run_job(job_id, session_id, question))
        self._tasks[job_id] = task
        task.add_done_callback(lambda _t, jid=job_id: self._tasks.pop(jid, None))
        return await self.db.get_research_job(job_id)

    # ------------------------------------------------------------------- run

    async def _run_job(self, job_id: str, session_id: str, question: str) -> None:
        assert self.session_mgr is not None and self.db is not None
        assert self._job_sem is not None
        try:
            async with self._job_sem:  # bound concurrent jobs
                session = self.session_mgr.get_session(session_id)
                if session is None:
                    raise ResearchError("session disappeared", status_code=404)
                harness = get_harness(session.backend)
                agent = await self.db.get_agent(session.agent_id) if session.agent_id else None
                model = (agent or {}).get("model")
                credential = await self.session_mgr.resolve_credential_by_id(
                    session.credential_id or (agent or {}).get("credential_id"),
                    style=harness.profile.credential_style,
                    context=f"research {job_id}",
                )

                async def on_progress(p: ResearchProgress) -> None:
                    await self.db.update_research_job(job_id, phase=p.phase)
                    await self._broadcast({
                        "type": "research_progress",
                        "session_id": session_id,
                        "research_id": job_id,
                        "phase": p.phase,
                        "detail": p.detail,
                        "counts": p.counts,
                    })

                # Run leaves in an EMPTY per-job scratch cwd, NOT the session's
                # working_dir — a web/reasoning leaf has no reason to read the
                # user's repo, and this keeps the read-only-sandbox/denylist
                # leaves from touching real files (Vera review).
                scratch = os.path.join(_research_dir(), job_id, "cwd")
                os.makedirs(scratch, exist_ok=True)
                report = await asyncio.wait_for(
                    run_research(
                        question,
                        harness=harness,
                        credential=credential,
                        model=model,
                        working_dir=scratch,
                        limits=ResearchLimits(),
                        on_progress=on_progress,
                    ),
                    timeout=settings.research_job_timeout_seconds,
                )

            # Persist the report file (best-effort) + mark completed.
            report_path = self._write_report(job_id, report.report)
            await self.db.update_research_job(
                job_id, status="completed", phase="done", cost=report.cost,
                completed_at=_now(), report_path=report_path,
            )
            await self._broadcast({
                "type": "research_completed",
                "session_id": session_id,
                "research_id": job_id,
                "sources": report.sources,
                "verified": len(report.findings),
            })
            await self._inject_report(job_id, session_id, question, report)
        except asyncio.CancelledError:
            await self._finalize_failed(job_id, session_id, "cancelled", "cancelled by user")
            raise
        except asyncio.TimeoutError:
            await self._finalize_failed(
                job_id, session_id, "failed",
                f"research exceeded {settings.research_job_timeout_seconds}s and was stopped",
            )
        except ResearchError as e:
            await self._finalize_failed(job_id, session_id, "failed", e.message)
        except Exception as e:  # noqa: BLE001
            logger.exception("research job %s crashed", job_id)
            await self._finalize_failed(job_id, session_id, "failed", str(e))

    async def _inject_report(
        self, job_id: str, session_id: str, question: str, report: Any
    ) -> None:
        """Deliver the report into the session as a turn (the bg-delivery path).
        Idempotent + tracked separately from completion (Vera review)."""
        assert self.db is not None and self.session_mgr is not None
        row = await self.db.get_research_job(job_id)
        if row and row.get("injection_status") == "delivered":
            return
        prompt = (
            f"[deep-research:{job_id}] Research complete for: {question}\n\n"
            f"{report.report}"
        )
        try:
            await self.session_mgr.start_message(session_id, prompt)
            await self.db.update_research_job(
                job_id, injection_status="delivered", injected_at=_now()
            )
        except Exception as e:  # noqa: BLE001 — parent may be gone; tolerate
            logger.warning("research %s: report injection failed: %s", job_id, e)
            await self.db.update_research_job(
                job_id, injection_status="failed", error=f"delivery failed: {e}"
            )

    async def _finalize_failed(
        self, job_id: str, session_id: str, status: str, error: str
    ) -> None:
        assert self.db is not None
        try:
            # Idempotent: if the job is already terminal (e.g. cancel() recorded
            # it before interrupting), don't overwrite or re-broadcast (Vera
            # review — avoids the cancel double-write/double-event).
            row = await self.db.get_research_job(job_id)
            if row and row["status"] != "running":
                return
            await self.db.update_research_job(
                job_id, status=status, error=error, completed_at=_now()
            )
            await self._broadcast({
                "type": "research_failed",
                "session_id": session_id,
                "research_id": job_id,
                "status": status,
                "error": error,
            })
        except Exception:
            logger.exception("research %s: finalize(%s) failed", job_id, status)

    # ---------------------------------------------------------------- cancel

    async def cancel(self, job_id: str) -> dict[str, Any]:
        if self.db is None:
            raise ResearchError("ResearchManager not bound", status_code=500)
        row = await self.db.get_research_job(job_id)
        if row is None:
            raise ResearchError(f"research job {job_id} not found", status_code=404)
        # Record cancelled + broadcast BEFORE interrupting live work, so the
        # state transition is authoritative and the REST caller/UI never see a
        # stale `running` (Vera review — cancel-as-state-transition). The
        # CancelledError path's _finalize_failed is now idempotent, so it won't
        # double-write. No-op if already terminal.
        if row["status"] == "running":
            await self.db.update_research_job(
                job_id, status="cancelled", error="cancelled by user",
                completed_at=_now(),
            )
            await self._broadcast({
                "type": "research_failed",
                "session_id": row["session_id"],
                "research_id": job_id,
                "status": "cancelled",
                "error": "cancelled by user",
            })
        task = self._tasks.get(job_id)
        if task and not task.done():
            task.cancel()  # reaps in-flight leaves via CancelledError
        return await self.db.get_research_job(job_id)

    # --------------------------------------------------------------- helpers

    def _write_report(self, job_id: str, report: str) -> str | None:
        try:
            path = os.path.join(_research_dir(), f"{job_id}.md")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(report)
            return path
        except Exception:
            logger.exception("research %s: could not write report file", job_id)
            return None

    async def _broadcast(self, msg: dict[str, Any]) -> None:
        if self.session_mgr is not None:
            try:
                await self.session_mgr._broadcast(msg)
            except Exception:
                logger.exception("research broadcast failed")


# Module-level singleton (mirrors session_manager / bg_tasks / delegations).
research_manager = ResearchManager()
