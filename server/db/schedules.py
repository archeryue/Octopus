"""Recurring prompts, owned by an agent.

One slice of the persistence layer. These were methods on a single 2,600-line
`Database` class; splitting them by domain (§3 A4) keeps each file about one
subject, while `Database` still presents them as one object so no call site
changed.
"""

from __future__ import annotations

from typing import Any

from .base import DatabaseBase


class SchedulesMixin(DatabaseBase):

    async def save_schedule(
        self,
        schedule_id: str,
        agent_id: str,
        name: str,
        prompt: str,
        created_at: str,
        interval_seconds: int | None = None,
        cron: str | None = None,
        timezone: str | None = None,
        recurrence_label: str | None = None,
        enabled: bool = True,
        origin_session_id: str | None = None,
        run_at: str | None = None,
    ) -> None:
        """Persist a schedule. Recurrence is one of `interval_seconds`, `cron`
        (with `timezone`), or `run_at` (ISO datetime, fires once then auto-deletes).
        `origin_session_id`, when set, is the session the `/schedule` command was
        typed in — fires append into it instead of a throwaway session."""
        await self._ensure_connected()
        await self.conn.execute(
            "INSERT INTO schedules (id, agent_id, origin_session_id, name, prompt, "
            "interval_seconds, cron, timezone, recurrence_label, enabled, "
            "created_at, run_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                schedule_id,
                agent_id,
                origin_session_id,
                name,
                prompt,
                interval_seconds,
                cron,
                timezone,
                recurrence_label,
                int(enabled),
                created_at,
                run_at,
            ),
        )
        await self.conn.commit()

    async def load_schedules(self) -> list[dict[str, Any]]:
        await self._ensure_connected()
        cursor = await self.conn.execute(
            "SELECT id, agent_id, name, prompt, interval_seconds, cron, timezone, "
            "recurrence_label, enabled, created_at, last_run_at, origin_session_id, "
            "run_at, last_run_session_id FROM schedules"
        )
        rows = await cursor.fetchall()
        return [
            {
                "id": row[0],
                "agent_id": row[1],
                "name": row[2],
                "prompt": row[3],
                "interval_seconds": row[4],
                "cron": row[5],
                "timezone": row[6],
                "recurrence_label": row[7],
                "enabled": bool(row[8]),
                "created_at": row[9],
                "last_run_at": row[10],
                "origin_session_id": row[11],
                "run_at": row[12],
                "last_run_session_id": row[13],
            }
            for row in rows
        ]

    async def delete_schedule(self, schedule_id: str) -> None:
        await self._ensure_connected()
        await self.conn.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,))
        await self.conn.commit()

    async def repoint_schedules_origin(
        self, old_session_id: str, new_session_id: str
    ) -> list[dict[str, Any]]:
        """Move every schedule anchored to `old_session_id` onto
        `new_session_id` (used when a session is archived and replaced — its
        schedules should keep appending into the live successor thread). Returns
        the affected schedule rows (post-update) so the caller can re-register
        their jobs. No-op returning [] when nothing points at the old session."""
        await self._ensure_connected()
        cursor = await self.conn.execute(
            "SELECT id FROM schedules WHERE origin_session_id = ?",
            (old_session_id,),
        )
        affected = {row[0] for row in await cursor.fetchall()}
        if not affected:
            return []
        await self.conn.execute(
            "UPDATE schedules SET origin_session_id = ? WHERE origin_session_id = ?",
            (new_session_id, old_session_id),
        )
        await self.conn.commit()
        return [r for r in await self.load_schedules() if r["id"] in affected]

    async def update_schedule(self, schedule_id: str, **fields: Any) -> None:
        await self._ensure_connected()
        allowed = {
            "name",
            "prompt",
            "interval_seconds",
            "cron",
            "run_at",
            "timezone",
            "recurrence_label",
            "enabled",
            "last_run_at",
            "last_run_session_id",
        }
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return
        if "enabled" in updates:
            updates["enabled"] = int(updates["enabled"])
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [schedule_id]
        await self.conn.execute(
            f"UPDATE schedules SET {set_clause} WHERE id = ?",
            values,
        )
        await self.conn.commit()
