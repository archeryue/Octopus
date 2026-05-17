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
    claude_session_id TEXT
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
    backend TEXT NOT NULL,         -- "claude-code" | "codex"
    label TEXT NOT NULL,
    auth_type TEXT NOT NULL,       -- "api_key" | "oauth"
    secret_encrypted TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_credentials_backend
  ON backend_credentials(backend);
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

    async def load_sessions(self) -> list[dict[str, Any]]:
        await self._ensure_connected()
        cursor = await self._conn.execute(
            "SELECT id, name, working_dir, created_at, claude_session_id, credential_id "
            "FROM sessions"
        )
        rows = await cursor.fetchall()
        return [
            {
                "id": row[0],
                "name": row[1],
                "working_dir": row[2],
                "created_at": row[3],
                "claude_session_id": row[4],
                "credential_id": row[5],
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
            "SELECT role, type, content, tool_name, tool_input, tool_use_id, "
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
            content = json.loads(row[2]) if row[2] is not None else None
            tool_input = json.loads(row[4]) if row[4] is not None else None
            is_error = bool(row[6]) if row[6] is not None else None
            results.append(
                {
                    "role": row[0],
                    "type": row[1],
                    "content": content,
                    "tool_name": row[3],
                    "tool_input": tool_input,
                    "tool_use_id": row[5],
                    "is_error": is_error,
                    "session_id": row[7],
                    "cost": row[8],
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
            "(id, backend, label, auth_type, secret_encrypted, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (credential_id, backend, label, auth_type, secret_encrypted, created_at),
        )
        await self._conn.commit()

    async def load_credentials(self) -> list[dict[str, Any]]:
        await self._ensure_connected()
        cursor = await self._conn.execute(
            "SELECT id, backend, label, auth_type, secret_encrypted, created_at "
            "FROM backend_credentials ORDER BY created_at"
        )
        rows = await cursor.fetchall()
        return [
            {
                "id": row[0],
                "backend": row[1],
                "label": row[2],
                "auth_type": row[3],
                "secret_encrypted": row[4],
                "created_at": row[5],
            }
            for row in rows
        ]

    async def get_credential(self, credential_id: str) -> dict[str, Any] | None:
        await self._ensure_connected()
        cursor = await self._conn.execute(
            "SELECT id, backend, label, auth_type, secret_encrypted, created_at "
            "FROM backend_credentials WHERE id = ?",
            (credential_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return {
            "id": row[0],
            "backend": row[1],
            "label": row[2],
            "auth_type": row[3],
            "secret_encrypted": row[4],
            "created_at": row[5],
        }

    async def update_credential(self, credential_id: str, **fields: Any) -> None:
        await self._ensure_connected()
        allowed = {"label", "secret_encrypted"}
        updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if not updates:
            return
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [credential_id]
        await self._conn.execute(
            f"UPDATE backend_credentials SET {set_clause} WHERE id = ?",
            values,
        )
        await self._conn.commit()

    async def delete_credential(self, credential_id: str) -> bool:
        await self._ensure_connected()
        cursor = await self._conn.execute(
            "DELETE FROM backend_credentials WHERE id = ?", (credential_id,)
        )
        await self._conn.commit()
        return cursor.rowcount > 0

    async def update_session_field(self, session_id: str, **fields: Any) -> None:
        await self._ensure_connected()
        allowed = {"name", "working_dir", "claude_session_id", "credential_id"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [session_id]
        await self._conn.execute(
            f"UPDATE sessions SET {set_clause} WHERE id = ?",
            values,
        )
        await self._conn.commit()
