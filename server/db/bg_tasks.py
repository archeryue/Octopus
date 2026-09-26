"""Background shell tasks that outlive the turn that started them.

One slice of the persistence layer. These were methods on a single 2,600-line
`Database` class; splitting them by domain (§3 A4) keeps each file about one
subject, while `Database` still presents them as one object so no call site
changed.
"""

from __future__ import annotations

from typing import Any

from .base import DatabaseBase


class BgTasksMixin(DatabaseBase):
    # --- Background tasks (cross-turn) ---


    async def create_bg_task(
        self,
        task_id: str,
        session_id: str,
        command: str,
        description: str | None,
        working_dir: str,
        started_at: str,
    ) -> None:
        await self._ensure_connected()
        await self.conn.execute(
            "INSERT INTO bg_tasks "
            "(id, session_id, command, description, working_dir, status, "
            " stdout, stderr, truncated, started_at) "
            "VALUES (?, ?, ?, ?, ?, 'running', '', '', 0, ?)",
            (task_id, session_id, command, description, working_dir, started_at),
        )
        await self.conn.commit()

    async def update_bg_task(self, task_id: str, **fields: Any) -> None:
        """Patch any of: status, exit_code, stdout, stderr, truncated, completed_at."""
        await self._ensure_connected()
        allowed = {
            "status",
            "exit_code",
            "stdout",
            "stderr",
            "truncated",
            "completed_at",
        }
        updates: dict[str, Any] = {}
        for k, v in fields.items():
            if k not in allowed:
                continue
            if k == "truncated":
                updates[k] = int(bool(v))
            else:
                updates[k] = v
        if not updates:
            return
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [task_id]
        await self.conn.execute(
            f"UPDATE bg_tasks SET {set_clause} WHERE id = ?", values
        )
        await self.conn.commit()

    async def get_bg_task(self, task_id: str) -> dict[str, Any] | None:
        await self._ensure_connected()
        cursor = await self.conn.execute(
            f"SELECT {self._BG_TASK_COLS} FROM bg_tasks WHERE id = ?", (task_id,)
        )
        row = await cursor.fetchone()
        return self._row_to_bg_task(row) if row else None

    async def mark_in_flight_bg_tasks_interrupted(
        self, completed_at: str
    ) -> int:
        """Called once at startup: any row left in `running` belongs to a
        prior FastAPI process that crashed or was restarted. The
        subprocess is gone (child of the dead parent), so the row is
        garbage — flip it to `interrupted` so the chat doesn't show a
        spinner that will never resolve. Returns rows updated.
        """
        await self._ensure_connected()
        cursor = await self.conn.execute(
            "UPDATE bg_tasks SET status = 'interrupted', completed_at = ? "
            "WHERE status IN ('running', 'pending')",
            (completed_at,),
        )
        await self.conn.commit()
        return cursor.rowcount
