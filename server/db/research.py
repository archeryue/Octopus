"""Deep-research jobs.

One slice of the persistence layer. These were methods on a single 2,600-line
`Database` class; splitting them by domain (§3 A4) keeps each file about one
subject, while `Database` still presents them as one object so no call site
changed.
"""

from __future__ import annotations

from typing import Any

from .base import DatabaseBase


class ResearchMixin(DatabaseBase):


    async def create_research_job(
        self, job_id: str, session_id: str, question: str, created_at: str
    ) -> None:
        await self._ensure_connected()
        await self.conn.execute(
            "INSERT INTO research_jobs "
            "(id, session_id, question, status, phase, created_at, injection_status) "
            "VALUES (?, ?, ?, 'running', 'scope', ?, 'pending')",
            (job_id, session_id, question, created_at),
        )
        await self.conn.commit()

    async def update_research_job(self, job_id: str, **fields: Any) -> None:
        """Patch any of: status, phase, error, report_path, cost, completed_at,
        injection_status, injected_at."""
        await self._ensure_connected()
        allowed = {
            "status", "phase", "error", "report_path", "cost", "completed_at",
            "injection_status", "injected_at",
        }
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        await self.conn.execute(
            f"UPDATE research_jobs SET {set_clause} WHERE id = ?",
            list(updates.values()) + [job_id],
        )
        await self.conn.commit()

    async def get_research_job(self, job_id: str) -> dict[str, Any] | None:
        await self._ensure_connected()
        cursor = await self.conn.execute(
            f"SELECT {self._RESEARCH_COLS} FROM research_jobs WHERE id = ?", (job_id,)
        )
        row = await cursor.fetchone()
        return self._row_to_research_job(row) if row else None

    async def mark_in_flight_research_jobs_interrupted(self, completed_at: str) -> int:
        """Boot sweep: a `running` row belongs to a prior process — its task is
        gone, so flip it to `interrupted` (native-deep-research.md §6). v1 does
        not resume mid-pipeline. Returns rows updated."""
        await self._ensure_connected()
        cursor = await self.conn.execute(
            "UPDATE research_jobs SET status = 'interrupted', completed_at = ? "
            "WHERE status = 'running'",
            (completed_at,),
        )
        await self.conn.commit()
        return cursor.rowcount
