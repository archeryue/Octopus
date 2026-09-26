"""Connector installations, their secrets, OAuth clients and custom kinds.

One slice of the persistence layer. These were methods on a single 2,600-line
`Database` class; splitting them by domain (§3 A4) keeps each file about one
subject, while `Database` still presents them as one object so no call site
changed.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from .base import DatabaseBase


class ConnectorsMixin(DatabaseBase):


    async def save_connector_installation(
        self,
        *,
        installation_id: str,
        kind: str,
        label: str,
        auth_type: str,
        secret_encrypted: str,
        created_at: str,
        external_account_id: str | None = None,
        scopes: list[str] | None = None,
        enable_by_default: bool = False,
        token_expires_at: str | None = None,
    ) -> None:
        await self._ensure_connected()
        await self.conn.execute(
            "INSERT INTO connector_installations "
            "(id, kind, label, auth_type, external_account_id, scopes, "
            " enable_by_default, needs_reconnect, token_expires_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
            (
                installation_id, kind, label, auth_type, external_account_id,
                json.dumps(scopes) if scopes is not None else None,
                int(bool(enable_by_default)), token_expires_at, created_at,
            ),
        )
        await self.conn.execute(
            "INSERT OR REPLACE INTO connector_installation_secrets "
            "(installation_id, secret_encrypted) VALUES (?, ?)",
            (installation_id, secret_encrypted),
        )
        await self.conn.commit()

    async def load_connector_installations(self) -> list[dict[str, Any]]:
        await self._ensure_connected()
        cursor = await self.conn.execute(
            f"SELECT {self._CONNECTOR_COLS} FROM connector_installations "
            "ORDER BY created_at"
        )
        rows = await cursor.fetchall()
        return [self._row_to_connector(row) for row in rows]

    async def get_connector_installation(
        self, installation_id: str
    ) -> dict[str, Any] | None:
        await self._ensure_connected()
        cursor = await self.conn.execute(
            f"SELECT {self._CONNECTOR_COLS} FROM connector_installations "
            "WHERE id = ?",
            (installation_id,),
        )
        row = await cursor.fetchone()
        return self._row_to_connector(row) if row else None

    async def get_connector_installation_by_account(
        self, kind: str, external_account_id: str
    ) -> dict[str, Any] | None:
        """Look up by (kind, external account) — the dedup key the install
        flow upserts on."""
        await self._ensure_connected()
        cursor = await self.conn.execute(
            f"SELECT {self._CONNECTOR_COLS} FROM connector_installations "
            "WHERE kind = ? AND external_account_id = ?",
            (kind, external_account_id),
        )
        row = await cursor.fetchone()
        return self._row_to_connector(row) if row else None

    async def get_connector_secret(self, installation_id: str) -> str | None:
        """The encrypted token blob — only the internal /token route reads
        this."""
        await self._ensure_connected()
        cursor = await self.conn.execute(
            "SELECT secret_encrypted FROM connector_installation_secrets "
            "WHERE installation_id = ?",
            (installation_id,),
        )
        row = await cursor.fetchone()
        return row[0] if row else None

    async def update_connector_installation(
        self, installation_id: str, **fields: Any
    ) -> None:
        await self._ensure_connected()
        meta_allowed = {
            "label",
            "external_account_id",
            "scopes",
            "enable_by_default",
            "needs_reconnect",
            "token_expires_at",
            "last_refresh_error_code",
        }
        # Nullable columns must be writable to NULL (e.g. clearing a stale
        # last_refresh_error_code after a good refresh). Columns omitted by
        # the caller are left untouched.
        nullable_meta = {
            "external_account_id",
            "scopes",
            "token_expires_at",
            "last_refresh_error_code",
        }
        meta_updates = {
            k: v
            for k, v in fields.items()
            if k in meta_allowed and (v is not None or k in nullable_meta)
        }
        if "scopes" in meta_updates and meta_updates["scopes"] is not None:
            meta_updates["scopes"] = json.dumps(meta_updates["scopes"])
        for boolish in ("enable_by_default", "needs_reconnect"):
            if boolish in meta_updates and meta_updates[boolish] is not None:
                meta_updates[boolish] = int(bool(meta_updates[boolish]))

        secret_value = fields.get("secret_encrypted")

        if meta_updates:
            set_clause = ", ".join(f"{k} = ?" for k in meta_updates)
            values = list(meta_updates.values()) + [installation_id]
            await self.conn.execute(
                f"UPDATE connector_installations SET {set_clause} WHERE id = ?",
                values,
            )

        if secret_value is not None:
            await self.conn.execute(
                "INSERT OR REPLACE INTO connector_installation_secrets "
                "(installation_id, secret_encrypted) VALUES (?, ?)",
                (installation_id, secret_value),
            )

        if meta_updates or secret_value is not None:
            await self.conn.commit()

    async def delete_connector_installation(self, installation_id: str) -> bool:
        await self._ensure_connected()
        # ON DELETE CASCADE drops the secret row and any agent_connectors links.
        cursor = await self.conn.execute(
            "DELETE FROM connector_installations WHERE id = ?", (installation_id,)
        )
        await self.conn.commit()
        return cursor.rowcount > 0
    # --- per-kind OAuth client credentials (in-app config) ----------------

    async def set_connector_oauth_client(
        self, kind: str, client_id: str, client_secret_encrypted: str, now: str
    ) -> None:
        await self._ensure_connected()
        await self.conn.execute(
            "INSERT INTO connector_oauth_clients "
            "(kind, client_id, client_secret_encrypted, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(kind) DO UPDATE SET "
            "client_id=excluded.client_id, "
            "client_secret_encrypted=excluded.client_secret_encrypted, "
            "updated_at=excluded.updated_at",
            (kind, client_id, client_secret_encrypted, now, now),
        )
        await self.conn.commit()

    async def get_connector_oauth_client(
        self, kind: str
    ) -> dict[str, Any] | None:
        await self._ensure_connected()
        cursor = await self.conn.execute(
            "SELECT kind, client_id, client_secret_encrypted "
            "FROM connector_oauth_clients WHERE kind = ?",
            (kind,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return {
            "kind": row[0],
            "client_id": row[1],
            "client_secret_encrypted": row[2],
        }

    async def delete_connector_oauth_client(self, kind: str) -> bool:
        await self._ensure_connected()
        cursor = await self.conn.execute(
            "DELETE FROM connector_oauth_clients WHERE kind = ?", (kind,)
        )
        await self.conn.commit()
        return cursor.rowcount > 0

    async def delete_connector_installations_by_kind(self, kind: str) -> int:
        """Delete every installation of a kind (cascades to secrets +
        agent_connectors). Used when a custom connector is removed."""
        await self._ensure_connected()
        cursor = await self.conn.execute(
            "DELETE FROM connector_installations WHERE kind = ?", (kind,)
        )
        await self.conn.commit()
        return cursor.rowcount
    # --- custom (user-defined) connector definitions ----------------------

    @staticmethod
    def _row_to_custom(row: sqlite3.Row) -> dict[str, Any]:
        # Columns: kind, display_name, authorize_url, token_url, scopes, pkce,
        # api_base, created_at, updated_at.
        try:
            scopes = json.loads(row[4]) if row[4] else []
        except (json.JSONDecodeError, TypeError):
            scopes = []
        return {
            "kind": row[0],
            "display_name": row[1],
            "authorize_url": row[2],
            "token_url": row[3],
            "scopes": scopes,
            "pkce": bool(row[5]),
            "api_base": row[6],
            "created_at": row[7],
            "updated_at": row[8],
        }

    async def save_custom_connector(
        self,
        *,
        kind: str,
        display_name: str,
        authorize_url: str,
        token_url: str,
        scopes: list[str],
        pkce: bool,
        api_base: str,
        now: str,
    ) -> None:
        await self._ensure_connected()
        await self.conn.execute(
            "INSERT INTO custom_connectors "
            "(kind, display_name, authorize_url, token_url, scopes, pkce, "
            " api_base, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(kind) DO UPDATE SET "
            "display_name=excluded.display_name, "
            "authorize_url=excluded.authorize_url, token_url=excluded.token_url, "
            "scopes=excluded.scopes, pkce=excluded.pkce, "
            "api_base=excluded.api_base, updated_at=excluded.updated_at",
            (
                kind, display_name, authorize_url, token_url,
                json.dumps(scopes), int(bool(pkce)), api_base, now, now,
            ),
        )
        await self.conn.commit()

    async def get_custom_connector(self, kind: str) -> dict[str, Any] | None:
        await self._ensure_connected()
        cursor = await self.conn.execute(
            f"SELECT {self._CUSTOM_COLS} FROM custom_connectors WHERE kind = ?",
            (kind,),
        )
        row = await cursor.fetchone()
        return self._row_to_custom(row) if row else None

    async def list_custom_connectors(self) -> list[dict[str, Any]]:
        await self._ensure_connected()
        cursor = await self.conn.execute(
            f"SELECT {self._CUSTOM_COLS} FROM custom_connectors ORDER BY created_at"
        )
        rows = await cursor.fetchall()
        return [self._row_to_custom(row) for row in rows]

    async def delete_custom_connector(self, kind: str) -> bool:
        await self._ensure_connected()
        cursor = await self.conn.execute(
            "DELETE FROM custom_connectors WHERE kind = ?", (kind,)
        )
        await self.conn.commit()
        return cursor.rowcount > 0
