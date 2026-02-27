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
"""


class Database:
    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def initialize(self) -> None:
        self._conn = await aiosqlite.connect(self._db_path)
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.executescript(_SCHEMA)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

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
    ) -> None:
        await self.conn.execute(
            "INSERT INTO sessions (id, name, working_dir, created_at, claude_session_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (session_id, name, working_dir, created_at, claude_session_id),
        )
        await self.conn.commit()

    async def delete_session(self, session_id: str) -> None:
        await self.conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        await self.conn.commit()

    async def load_sessions(self) -> list[dict[str, Any]]:
        cursor = await self.conn.execute(
            "SELECT id, name, working_dir, created_at, claude_session_id FROM sessions"
        )
        rows = await cursor.fetchall()
        return [
            {
                "id": row[0],
                "name": row[1],
                "working_dir": row[2],
                "created_at": row[3],
                "claude_session_id": row[4],
            }
            for row in rows
        ]

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
        content_str = json.dumps(content) if content is not None else None
        tool_input_str = json.dumps(tool_input) if tool_input is not None else None
        is_error_int = int(is_error) if is_error is not None else None

        await self.conn.execute(
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
        await self.conn.commit()

    async def load_messages(self, session_id: str) -> list[dict[str, Any]]:
        cursor = await self.conn.execute(
            "SELECT role, type, content, tool_name, tool_input, tool_use_id, "
            "is_error, session_id_ref, cost "
            "FROM messages WHERE session_id = ? ORDER BY seq",
            (session_id,),
        )
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

    async def update_session_field(self, session_id: str, **fields: Any) -> None:
        allowed = {"name", "working_dir", "claude_session_id"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [session_id]
        await self.conn.execute(
            f"UPDATE sessions SET {set_clause} WHERE id = ?",
            values,
        )
        await self.conn.commit()
