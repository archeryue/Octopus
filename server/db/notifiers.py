"""Outbound notifiers.

One slice of the persistence layer. These were methods on a single 2,600-line
`Database` class; splitting them by domain (§3 A4) keeps each file about one
subject, while `Database` still presents them as one object so no call site
changed.
"""

from __future__ import annotations

import json
from typing import Any

from .base import DatabaseBase


class NotifiersMixin(DatabaseBase):
    # --- Notifiers ---

    async def save_notifier(
        self,
        notifier_id: str,
        type: str,
        label: str,
        config: dict[str, Any],
        created_at: str,
        enabled: bool = True,
    ) -> None:
        await self._ensure_connected()
        await self.conn.execute(
            "INSERT INTO notifiers (id, type, label, config, enabled, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (notifier_id, type, label, json.dumps(config), int(enabled), created_at),
        )
        await self.conn.commit()

    async def load_notifiers(self) -> list[dict[str, Any]]:
        await self._ensure_connected()
        cursor = await self.conn.execute(
            "SELECT id, type, label, config, enabled, created_at "
            "FROM notifiers ORDER BY created_at"
        )
        rows = await cursor.fetchall()
        return [
            {
                "id": row[0],
                "type": row[1],
                "label": row[2],
                "config": json.loads(row[3]) if row[3] else {},
                "enabled": bool(row[4]),
                "created_at": row[5],
            }
            for row in rows
        ]

    async def delete_notifier(self, notifier_id: str) -> bool:
        await self._ensure_connected()
        cursor = await self.conn.execute(
            "DELETE FROM notifiers WHERE id = ?", (notifier_id,)
        )
        await self.conn.commit()
        return cursor.rowcount > 0

    async def update_notifier(self, notifier_id: str, **fields: Any) -> None:
        await self._ensure_connected()
        allowed = {"label", "enabled", "config"}
        updates: dict[str, Any] = {}
        for k, v in fields.items():
            if k not in allowed or v is None:
                continue
            if k == "enabled":
                updates[k] = int(bool(v))
            elif k == "config":
                updates[k] = json.dumps(v)
            else:
                updates[k] = v
        if not updates:
            return
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [notifier_id]
        await self.conn.execute(
            f"UPDATE notifiers SET {set_clause} WHERE id = ?", values
        )
        await self.conn.commit()
