from __future__ import annotations

import json
import logging
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    working_dir TEXT NOT NULL,
    created_at TEXT NOT NULL,
    claude_session_id TEXT,
    archived INTEGER NOT NULL DEFAULT 0  -- hidden from default list; row kept for history
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    role TEXT NOT NULL,
    type TEXT NOT NULL,
    content TEXT,
    tool_name TEXT,
    tool_input TEXT,
    tool_use_id TEXT,
    is_error INTEGER,
    session_id_ref TEXT,
    cost REAL,
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, seq);

CREATE TABLE IF NOT EXISTS schedules (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    name TEXT NOT NULL,
    prompt TEXT NOT NULL,
    interval_seconds INTEGER NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    last_run_at TEXT,
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS bridge_mappings (
    platform TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    PRIMARY KEY (platform, chat_id),
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS backend_credentials (
    id TEXT PRIMARY KEY,
    backend TEXT NOT NULL,                 -- "claude-code" | "codex" | …
    label TEXT NOT NULL,
    auth_type TEXT NOT NULL,               -- "api_key" | "oauth"
    secret_encrypted TEXT NOT NULL,        -- LEGACY: kept for back-compat reads
                                           -- during the storage-split rollout.
                                           -- New writes go into credential_secrets.
    created_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active', -- "active" | "needs_reconnect"
    token_expires_at TEXT,                 -- ISO8601, null for non-expiring keys
    needs_reconnect INTEGER NOT NULL DEFAULT 0,
    last_refresh_error_code TEXT           -- see oauth_errors.RefreshErrorCode
);

CREATE INDEX IF NOT EXISTS idx_credentials_backend
  ON backend_credentials(backend);

-- Storage split (Steal Plan B-4): secrets live in their own table so a
-- future `serverOnly` flag can keep refresh tokens out of subprocess env,
-- and so we can join-or-not on the encrypted blob depending on the caller.
CREATE TABLE IF NOT EXISTS credential_secrets (
    credential_id TEXT PRIMARY KEY,
    secret_encrypted TEXT NOT NULL,
    FOREIGN KEY (credential_id) REFERENCES backend_credentials(id)
        ON DELETE CASCADE
);

-- Async notification targets (future-features #5). Each row is one
-- destination Octopus can poke when a session transitions to idle
-- (and, later, when an AskUserQuestion is pending / a schedule fails).
-- `config` is a JSON blob whose shape depends on `type` (e.g. for
-- type='webhook': {"url": "https://…"}).
CREATE TABLE IF NOT EXISTS notifiers (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,                    -- 'webhook' | future: 'email', 'browser_push'
    label TEXT NOT NULL,
    config TEXT NOT NULL,                  -- JSON
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
"""


class Database:
    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._conn: aiosqlite.Connection | None = None
        self._dirty: bool = False

    async def initialize(self) -> None:
        self._conn = await aiosqlite.connect(self._db_path)
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.executescript(_SCHEMA)
        await self._apply_migrations()
        await self._conn.commit()

    async def _apply_migrations(self) -> None:
        """Idempotent additive migrations for tables that pre-existed."""
        # sessions.credential_id was added when per-backend auth landed.
        try:
            await self._conn.execute(
                "ALTER TABLE sessions ADD COLUMN credential_id TEXT"
            )
        except Exception:
            # Column already exists — SQLite has no IF NOT EXISTS for ALTER COLUMN
            pass

        # sessions.archived for /archive feature (hides old session row from
        # the default list, keeps it in DB so it could be surfaced later).
        try:
            await self._conn.execute(
                "ALTER TABLE sessions ADD COLUMN archived INTEGER NOT NULL DEFAULT 0"
            )
        except Exception:
            pass

        # backend_credentials gained status / refresh-tracking columns (B-4/B-5).
        # Each ALTER is wrapped because SQLite has no IF NOT EXISTS for them.
        for ddl in (
            "ALTER TABLE backend_credentials ADD COLUMN "
            "status TEXT NOT NULL DEFAULT 'active'",
            "ALTER TABLE backend_credentials ADD COLUMN token_expires_at TEXT",
            "ALTER TABLE backend_credentials ADD COLUMN "
            "needs_reconnect INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE backend_credentials ADD COLUMN "
            "last_refresh_error_code TEXT",
        ):
            try:
                await self._conn.execute(ddl)
            except Exception:
                pass

        # Storage split (B-4): copy any existing legacy secrets into the
        # dedicated credential_secrets table. New writes go there directly;
        # this catch-up only runs once per pre-split row.
        try:
            await self._conn.execute(
                "INSERT OR IGNORE INTO credential_secrets "
                "(credential_id, secret_encrypted) "
                "SELECT id, secret_encrypted FROM backend_credentials"
            )
        except Exception:
            logger.exception("credential storage-split backfill failed")

    async def _ensure_connected(self) -> None:
        if self._conn is None:
            logger.warning("Database connection lost, reconnecting...")
            self._conn = await aiosqlite.connect(self._db_path)
            await self._conn.execute("PRAGMA journal_mode=WAL")
            await self._conn.execute("PRAGMA foreign_keys=ON")

    async def close(self) -> None:
        if self._conn:
            if self._dirty:
                await self._conn.commit()
                self._dirty = False
            await self._conn.close()
            self._conn = None

    async def flush(self) -> None:
        """Commit pending writes."""
        await self._ensure_connected()
        if self._dirty:
            await self._conn.commit()
            self._dirty = False

    @property
    def conn(self) -> aiosqlite.Connection:
        assert self._conn is not None, "Database not initialized"
        return self._conn

    async def save_session(
        self,
        session_id: str,
        name: str,
        working_dir: str,
        created_at: str,
        claude_session_id: str | None = None,
        credential_id: str | None = None,
    ) -> None:
        await self._ensure_connected()
        await self._conn.execute(
            "INSERT INTO sessions "
            "(id, name, working_dir, created_at, claude_session_id, credential_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                session_id,
                name,
                working_dir,
                created_at,
                claude_session_id,
                credential_id,
            ),
        )
        await self._conn.commit()

    async def delete_session(self, session_id: str) -> None:
        await self._ensure_connected()
        await self._conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        await self._conn.commit()

    async def load_sessions(
        self, *, include_archived: bool = False
    ) -> list[dict[str, Any]]:
        await self._ensure_connected()
        query = (
            "SELECT id, name, working_dir, created_at, claude_session_id, "
            "credential_id, archived FROM sessions"
        )
        if not include_archived:
            query += " WHERE archived = 0"
        cursor = await self._conn.execute(query)
        rows = await cursor.fetchall()
        return [
            {
                "id": row[0],
                "name": row[1],
                "working_dir": row[2],
                "created_at": row[3],
                "claude_session_id": row[4],
                "credential_id": row[5],
                "archived": bool(row[6]),
            }
            for row in rows
        ]

    async def count_messages(self, session_id: str) -> int:
        await self._ensure_connected()
        cursor = await self._conn.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id = ?", (session_id,)
        )
        row = await cursor.fetchone()
        return row[0]

    async def append_message(
        self,
        session_id: str,
        seq: int,
        role: str,
        type: str,
        content: Any = None,
        tool_name: str | None = None,
        tool_input: dict[str, Any] | None = None,
        tool_use_id: str | None = None,
        is_error: bool | None = None,
        session_id_ref: str | None = None,
        cost: float | None = None,
    ) -> None:
        await self._ensure_connected()
        content_str = json.dumps(content) if content is not None else None
        tool_input_str = json.dumps(tool_input) if tool_input is not None else None
        is_error_int = int(is_error) if is_error is not None else None

        await self._conn.execute(
            "INSERT INTO messages "
            "(session_id, seq, role, type, content, tool_name, tool_input, "
            "tool_use_id, is_error, session_id_ref, cost) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session_id,
                seq,
                role,
                type,
                content_str,
                tool_name,
                tool_input_str,
                tool_use_id,
                is_error_int,
                session_id_ref,
                cost,
            ),
        )
        self._dirty = True

    async def load_messages(
        self, session_id: str, limit: int = 0, offset: int = 0
    ) -> list[dict[str, Any]]:
        await self._ensure_connected()
        await self.flush()  # ensure pending writes are visible
        query = (
            "SELECT seq, role, type, content, tool_name, tool_input, tool_use_id, "
            "is_error, session_id_ref, cost "
            "FROM messages WHERE session_id = ? ORDER BY seq"
        )
        params: list = [session_id]
        if limit > 0:
            query += " LIMIT ? OFFSET ?"
            params.extend([limit, offset])
        cursor = await self._conn.execute(query, params)
        rows = await cursor.fetchall()
        results = []
        for row in rows:
            content = json.loads(row[3]) if row[3] is not None else None
            tool_input = json.loads(row[5]) if row[5] is not None else None
            is_error = bool(row[7]) if row[7] is not None else None
            results.append(
                {
                    "seq": row[0],
                    "role": row[1],
                    "type": row[2],
                    "content": content,
                    "tool_name": row[4],
                    "tool_input": tool_input,
                    "tool_use_id": row[6],
                    "is_error": is_error,
                    "session_id": row[8],
                    "cost": row[9],
                }
            )
        return results

    # --- Bridge mappings ---

    async def save_bridge_mapping(
        self, platform: str, chat_id: str, session_id: str
    ) -> None:
        await self._ensure_connected()
        await self._conn.execute(
            "INSERT OR REPLACE INTO bridge_mappings (platform, chat_id, session_id) "
            "VALUES (?, ?, ?)",
            (platform, chat_id, session_id),
        )
        await self._conn.commit()

    async def delete_bridge_mapping(self, platform: str, chat_id: str) -> None:
        await self._ensure_connected()
        await self._conn.execute(
            "DELETE FROM bridge_mappings WHERE platform = ? AND chat_id = ?",
            (platform, chat_id),
        )
        await self._conn.commit()

    async def load_bridge_mappings(self) -> list[dict[str, str]]:
        await self._ensure_connected()
        cursor = await self._conn.execute(
            "SELECT platform, chat_id, session_id FROM bridge_mappings"
        )
        rows = await cursor.fetchall()
        return [
            {"platform": row[0], "chat_id": row[1], "session_id": row[2]}
            for row in rows
        ]

    # --- Schedules ---

    async def save_schedule(
        self,
        schedule_id: str,
        session_id: str,
        name: str,
        prompt: str,
        interval_seconds: int,
        created_at: str,
        enabled: bool = True,
    ) -> None:
        await self._ensure_connected()
        await self._conn.execute(
            "INSERT INTO schedules (id, session_id, name, prompt, interval_seconds, enabled, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (schedule_id, session_id, name, prompt, interval_seconds, int(enabled), created_at),
        )
        await self._conn.commit()

    async def load_schedules(self) -> list[dict[str, Any]]:
        await self._ensure_connected()
        cursor = await self._conn.execute(
            "SELECT id, session_id, name, prompt, interval_seconds, enabled, created_at, last_run_at "
            "FROM schedules"
        )
        rows = await cursor.fetchall()
        return [
            {
                "id": row[0],
                "session_id": row[1],
                "name": row[2],
                "prompt": row[3],
                "interval_seconds": row[4],
                "enabled": bool(row[5]),
                "created_at": row[6],
                "last_run_at": row[7],
            }
            for row in rows
        ]

    async def delete_schedule(self, schedule_id: str) -> None:
        await self._ensure_connected()
        await self._conn.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,))
        await self._conn.commit()

    async def update_schedule(self, schedule_id: str, **fields: Any) -> None:
        await self._ensure_connected()
        allowed = {"name", "prompt", "interval_seconds", "enabled", "last_run_at"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return
        if "enabled" in updates:
            updates["enabled"] = int(updates["enabled"])
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [schedule_id]
        await self._conn.execute(
            f"UPDATE schedules SET {set_clause} WHERE id = ?",
            values,
        )
        await self._conn.commit()

    # --- Backend credentials ---

    # Credentials are stored across two tables (Steal Plan B-4):
    #   - `backend_credentials` holds metadata + refresh-state columns
    #   - `credential_secrets` holds only the encrypted blob
    # We still write `backend_credentials.secret_encrypted` for back-compat
    # in case anything downstream reads the legacy column; new code should
    # treat `credential_secrets.secret_encrypted` as the source of truth.

    _CREDENTIAL_COLS = (
        "c.id",
        "c.backend",
        "c.label",
        "c.auth_type",
        "COALESCE(s.secret_encrypted, c.secret_encrypted) AS secret_encrypted",
        "c.created_at",
        "c.status",
        "c.token_expires_at",
        "c.needs_reconnect",
        "c.last_refresh_error_code",
    )

    def _row_to_credential(self, row: tuple[Any, ...]) -> dict[str, Any]:
        return {
            "id": row[0],
            "backend": row[1],
            "label": row[2],
            "auth_type": row[3],
            "secret_encrypted": row[4],
            "created_at": row[5],
            "status": row[6] or "active",
            "token_expires_at": row[7],
            "needs_reconnect": bool(row[8]),
            "last_refresh_error_code": row[9],
        }

    async def save_credential(
        self,
        credential_id: str,
        backend: str,
        label: str,
        auth_type: str,
        secret_encrypted: str,
        created_at: str,
    ) -> None:
        await self._ensure_connected()
        await self._conn.execute(
            "INSERT INTO backend_credentials "
            "(id, backend, label, auth_type, secret_encrypted, created_at, "
            " status, needs_reconnect) "
            "VALUES (?, ?, ?, ?, ?, ?, 'active', 0)",
            (credential_id, backend, label, auth_type, secret_encrypted, created_at),
        )
        await self._conn.execute(
            "INSERT OR REPLACE INTO credential_secrets "
            "(credential_id, secret_encrypted) VALUES (?, ?)",
            (credential_id, secret_encrypted),
        )
        await self._conn.commit()

    async def load_credentials(self) -> list[dict[str, Any]]:
        await self._ensure_connected()
        cols = ", ".join(self._CREDENTIAL_COLS)
        cursor = await self._conn.execute(
            f"SELECT {cols} FROM backend_credentials c "
            "LEFT JOIN credential_secrets s ON s.credential_id = c.id "
            "ORDER BY c.created_at"
        )
        rows = await cursor.fetchall()
        return [self._row_to_credential(row) for row in rows]

    async def get_credential(self, credential_id: str) -> dict[str, Any] | None:
        await self._ensure_connected()
        cols = ", ".join(self._CREDENTIAL_COLS)
        cursor = await self._conn.execute(
            f"SELECT {cols} FROM backend_credentials c "
            "LEFT JOIN credential_secrets s ON s.credential_id = c.id "
            "WHERE c.id = ?",
            (credential_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return self._row_to_credential(row)

    async def update_credential(self, credential_id: str, **fields: Any) -> None:
        await self._ensure_connected()
        meta_allowed = {
            "label",
            "status",
            "token_expires_at",
            "needs_reconnect",
            "last_refresh_error_code",
        }
        # Nullable columns need to be writable to NULL (e.g. clearing a
        # stale `last_refresh_error_code` after a successful refresh).
        # Callers that want to leave a column alone should just not pass it.
        nullable_meta = {"token_expires_at", "last_refresh_error_code"}
        meta_updates = {
            k: v
            for k, v in fields.items()
            if k in meta_allowed and (v is not None or k in nullable_meta)
        }
        if "needs_reconnect" in meta_updates and meta_updates["needs_reconnect"] is not None:
            meta_updates["needs_reconnect"] = int(bool(meta_updates["needs_reconnect"]))

        secret_value = fields.get("secret_encrypted")

        if meta_updates:
            # Legacy column gets the same secret to keep readers consistent
            # if they bypass the JOIN.
            applied = dict(meta_updates)
            if secret_value is not None:
                applied["secret_encrypted"] = secret_value
            set_clause = ", ".join(f"{k} = ?" for k in applied)
            values = list(applied.values()) + [credential_id]
            await self._conn.execute(
                f"UPDATE backend_credentials SET {set_clause} WHERE id = ?",
                values,
            )
        elif secret_value is not None:
            await self._conn.execute(
                "UPDATE backend_credentials SET secret_encrypted = ? WHERE id = ?",
                (secret_value, credential_id),
            )

        if secret_value is not None:
            await self._conn.execute(
                "INSERT OR REPLACE INTO credential_secrets "
                "(credential_id, secret_encrypted) VALUES (?, ?)",
                (credential_id, secret_value),
            )

        if meta_updates or secret_value is not None:
            await self._conn.commit()

    async def delete_credential(self, credential_id: str) -> bool:
        await self._ensure_connected()
        # ON DELETE CASCADE on credential_secrets handles the secret row.
        cursor = await self._conn.execute(
            "DELETE FROM backend_credentials WHERE id = ?", (credential_id,)
        )
        await self._conn.commit()
        return cursor.rowcount > 0

    async def update_session_field(self, session_id: str, **fields: Any) -> None:
        await self._ensure_connected()
        allowed = {
            "name",
            "working_dir",
            "claude_session_id",
            "credential_id",
            "archived",
        }
        updates: dict[str, Any] = {}
        for k, v in fields.items():
            if k not in allowed:
                continue
            updates[k] = int(bool(v)) if k == "archived" else v
        if not updates:
            return
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [session_id]
        await self._conn.execute(
            f"UPDATE sessions SET {set_clause} WHERE id = ?",
            values,
        )
        await self._conn.commit()

    async def repoint_schedules(self, old_id: str, new_id: str) -> int:
        """Move all schedules attached to old_id over to new_id.

        Used by /archive: the user's automation is bound to the
        logical "session", not to its historical message chain.
        Returns rows updated.
        """
        await self._ensure_connected()
        cursor = await self._conn.execute(
            "UPDATE schedules SET session_id = ? WHERE session_id = ?",
            (new_id, old_id),
        )
        await self._conn.commit()
        return cursor.rowcount

    async def repoint_bridge_mappings(self, old_id: str, new_id: str) -> int:
        """Same logic as repoint_schedules for the bridge-mapping table.

        Telegram chats etc. continue routing into whichever session is
        the live one after an /archive.
        """
        await self._ensure_connected()
        cursor = await self._conn.execute(
            "UPDATE bridge_mappings SET session_id = ? WHERE session_id = ?",
            (new_id, old_id),
        )
        await self._conn.commit()
        return cursor.rowcount

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
        await self._conn.execute(
            "INSERT INTO notifiers (id, type, label, config, enabled, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (notifier_id, type, label, json.dumps(config), int(enabled), created_at),
        )
        await self._conn.commit()

    async def load_notifiers(self) -> list[dict[str, Any]]:
        await self._ensure_connected()
        cursor = await self._conn.execute(
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
        cursor = await self._conn.execute(
            "DELETE FROM notifiers WHERE id = ?", (notifier_id,)
        )
        await self._conn.commit()
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
        await self._conn.execute(
            f"UPDATE notifiers SET {set_clause} WHERE id = ?", values
        )
        await self._conn.commit()
