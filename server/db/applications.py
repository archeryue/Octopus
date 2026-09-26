"""Applications — the agent-built web apps.

One slice of the persistence layer. These were methods on a single 2,600-line
`Database` class; splitting them by domain (§3 A4) keeps each file about one
subject, while `Database` still presents them as one object so no call site
changed.
"""

from __future__ import annotations

from typing import Any

from .base import DatabaseBase


class ApplicationsMixin(DatabaseBase):


    async def save_application(
        self,
        *,
        app_id: str,
        name: str,
        app_dir: str,
        created_at: str,
        updated_at: str,
        description: str = "",
        icon: str | None = None,
        agent_id: str | None = None,
        session_id: str | None = None,
        entrypoint: str = "index.html",
        status: str = "building",
    ) -> None:
        await self._ensure_connected()
        await self.conn.execute(
            "INSERT INTO applications "
            "(id, name, description, icon, agent_id, session_id, app_dir, "
            " entrypoint, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                app_id,
                name,
                description,
                icon,
                agent_id,
                session_id,
                app_dir,
                entrypoint,
                status,
                created_at,
                updated_at,
            ),
        )
        await self.conn.commit()

    async def load_applications(
        self, *, include_archived: bool = False, only_archived: bool = False
    ) -> list[dict[str, Any]]:
        await self._ensure_connected()
        where = ""
        if only_archived:
            where = " WHERE archived = 1"
        elif not include_archived:
            where = " WHERE archived = 0"
        cursor = await self.conn.execute(
            f"SELECT {self._APPLICATION_COLS} FROM applications{where} "
            "ORDER BY created_at ASC"
        )
        rows = await cursor.fetchall()
        return [self._row_to_application(r) for r in rows]

    async def get_application(self, app_id: str) -> dict[str, Any] | None:
        await self._ensure_connected()
        cursor = await self.conn.execute(
            f"SELECT {self._APPLICATION_COLS} FROM applications WHERE id = ?",
            (app_id,),
        )
        row = await cursor.fetchone()
        return self._row_to_application(row) if row else None

    async def get_application_by_name(
        self, name: str, *, include_archived: bool = False
    ) -> dict[str, Any] | None:
        """Live applications only by default — an archived app doesn't hold its
        name (the unique index is live-only), so creating a replacement with
        the same name is allowed."""
        await self._ensure_connected()
        query = (
            f"SELECT {self._APPLICATION_COLS} FROM applications "
            "WHERE name = ? COLLATE NOCASE"
        )
        if not include_archived:
            query += " AND archived = 0"
        cursor = await self.conn.execute(query, (name,))
        row = await cursor.fetchone()
        return self._row_to_application(row) if row else None

    async def update_application(self, app_id: str, **fields: Any) -> None:
        """Patch any of: name, description, icon, icon_src, agent_id,
        session_id, entrypoint, status, error, updated_at, last_built_at.

        Anything not on the list is ignored rather than written — which is why
        `icon_src` has to be added here explicitly even though only `_evaluate`
        ever sets it. (The API models keep it out of Create/Update, so this
        list is not the thing protecting it from clients.)
        """
        await self._ensure_connected()
        allowed = {
            "name", "description", "icon", "icon_src", "agent_id",
            "session_id", "entrypoint", "status", "error", "archived",
            "updated_at", "last_built_at",
        }
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        await self.conn.execute(
            f"UPDATE applications SET {set_clause} WHERE id = ?",
            list(updates.values()) + [app_id],
        )
        await self.conn.commit()

    async def delete_application(self, app_id: str) -> bool:
        await self._ensure_connected()
        cursor = await self.conn.execute(
            "DELETE FROM applications WHERE id = ?", (app_id,)
        )
        await self.conn.commit()
        return cursor.rowcount > 0
