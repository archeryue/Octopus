from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)

# Built-in MCP servers attached to the Default Agent (and the default for
# any newly-created agent). Kept here so the migration backfill and the
# CREATE TABLE default stay in lock-step.
_DEFAULT_MCP_SERVERS = ["ask", "bg", "viewer"]
_DEFAULT_MCP_SERVERS_JSON = json.dumps(_DEFAULT_MCP_SERVERS)

_SCHEMA = """
-- Agents are the durable definition of an assistant (agent-refactor.md §4.1):
-- identity + system prompt + model + credential + built-in MCP set + tool
-- policy. They OWN sessions, schedules and bridge bindings. Memory (the
-- north star) hangs off the agent_id later; not in this refactor.
CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY,                    -- 12-char hex, same scheme as sessions
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    avatar TEXT,                            -- emoji or URL, optional
    system_prompt TEXT NOT NULL DEFAULT '',
    model TEXT,                             -- e.g. "claude-opus-4-7"; null = backend default
    credential_id TEXT REFERENCES backend_credentials(id) ON DELETE SET NULL,
    mcp_servers TEXT NOT NULL DEFAULT '["ask","bg","viewer"]',
                                            -- JSON array of built-in Octopus MCP server ids.
    tool_allow TEXT NOT NULL DEFAULT '',    -- newline-separated tool/MCP names; empty = allow all
    tool_deny  TEXT NOT NULL DEFAULT '',    -- newline-separated; deny takes precedence over allow
    is_system INTEGER NOT NULL DEFAULT 0,   -- 1 = the protected Default Agent (cannot be deleted)
    archived INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS agents_name_unique ON agents(name) WHERE archived = 0;

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    working_dir TEXT NOT NULL,
    created_at TEXT NOT NULL,
    claude_session_id TEXT,                -- backend resume id: a Claude session id
                                           -- OR a Codex thread_id (backend-agnostic;
                                           -- name kept for back-compat — codex-backend.md §4.3)
    archived INTEGER NOT NULL DEFAULT 0,   -- hidden from default list; row kept for history
    agent_id TEXT REFERENCES agents(id) ON DELETE CASCADE,  -- owner; nullable in SQLite, required by API
    origin TEXT NOT NULL DEFAULT 'user',   -- 'user' | 'schedule' | 'bridge'
    backend TEXT NOT NULL DEFAULT 'claude-code'  -- 'claude-code' | 'codex' (codex-backend.md §4.1)
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
    attachments TEXT,                       -- JSON list[AttachmentMetadata], null when none
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, seq);

-- A schedule belongs to the Agent ("every morning, summarize my inbox"),
-- not to a throwaway thread. Each fire materializes a fresh session under
-- the agent (scheduler.py). No persistent session_id here anymore.
CREATE TABLE IF NOT EXISTS schedules (
    id TEXT PRIMARY KEY,
    agent_id TEXT REFERENCES agents(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    prompt TEXT NOT NULL,
    interval_seconds INTEGER NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    last_run_at TEXT
);

-- (platform, chat_id) binds durably to an AGENT. session_id is demoted to a
-- sticky pointer at the currently-open thread (nullable; rolls as sessions
-- come and go). A chat that has never opened a session has session_id NULL.
CREATE TABLE IF NOT EXISTS bridge_mappings (
    platform TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    agent_id TEXT REFERENCES agents(id) ON DELETE CASCADE,
    session_id TEXT,
    PRIMARY KEY (platform, chat_id),
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE SET NULL
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

-- Cross-turn background tasks. The model calls `bg_run(cmd)` via the
-- bg MCP server; we persist a row here, spawn the subprocess, and on
-- completion synthesize a follow-up user message in the session so the
-- model is told "your bg task finished, here's the result" in its next
-- turn. The whole point is that the bg subprocess lives in the
-- long-running FastAPI process — independent of any one claude --print
-- invocation — so it survives turn boundaries the way Bash's
-- run_in_background does not.
--
-- stdout/stderr are capped (see server.bg_tasks.MAX_STREAM_BYTES);
-- excess content is truncated from the head with a `…[truncated N bytes]`
-- prefix so the model sees the most recent output.
CREATE TABLE IF NOT EXISTS bg_tasks (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    command TEXT NOT NULL,
    description TEXT,
    working_dir TEXT NOT NULL,
    status TEXT NOT NULL,                  -- 'pending'|'running'|'completed'|'failed'|'cancelled'|'interrupted'
    exit_code INTEGER,
    stdout TEXT NOT NULL DEFAULT '',
    stderr TEXT NOT NULL DEFAULT '',
    truncated INTEGER NOT NULL DEFAULT 0,  -- bool: at least one stream hit the cap
    started_at TEXT NOT NULL,
    completed_at TEXT,
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_bg_tasks_session
  ON bg_tasks(session_id, started_at);
"""


class Database:
    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._conn: aiosqlite.Connection | None = None
        self._dirty: bool = False
        self._closed: bool = False

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

        # messages.attachments was added with the file/image upload feature.
        try:
            await self._conn.execute(
                "ALTER TABLE messages ADD COLUMN attachments TEXT"
            )
        except Exception:
            pass

        # sessions.backend ('claude-code' | 'codex') — codex-backend.md §4.1.
        # DEFAULT backfills existing rows to claude-code → no behavior change.
        try:
            await self._conn.execute(
                "ALTER TABLE sessions ADD COLUMN backend TEXT NOT NULL "
                "DEFAULT 'claude-code'"
            )
        except Exception:
            pass

        await self._migrate_agents()

    async def _column_info(self, table: str) -> list[tuple[Any, ...]]:
        cursor = await self._conn.execute(f"PRAGMA table_info({table})")
        return list(await cursor.fetchall())

    async def _has_column(self, table: str, column: str) -> bool:
        return any(row[1] == column for row in await self._column_info(table))

    async def _column_is_not_null(self, table: str, column: str) -> bool:
        # PRAGMA table_info row: (cid, name, type, notnull, dflt_value, pk)
        for row in await self._column_info(table):
            if row[1] == column:
                return bool(row[3])
        return False

    async def _migrate_agents(self) -> None:
        """First-class Agents refactor migration (agent-refactor.md §4.5).

        Adds agent ownership to sessions / schedules / bridge_mappings,
        creates the protected Default Agent, and backfills every
        pre-existing row to it. Idempotent: safe on every boot, a second
        run no-ops (system agent present, no null agent_id rows, the
        column-shape rebuilds already applied). `schedules.session_id`
        and `bridge_mappings`' NOT NULL `session_id` are removed by
        table-rebuild rather than ALTER … DROP/MODIFY, because SQLite
        forbids dropping a column that's part of a foreign key and can't
        relax NOT NULL in place.
        """
        # 1. Additive columns (wrapped — SQLite has no IF NOT EXISTS for ALTER).
        #    Adding a column with a REFERENCES clause is allowed because the
        #    default value is NULL.
        for ddl in (
            "ALTER TABLE sessions ADD COLUMN agent_id TEXT "
            "REFERENCES agents(id) ON DELETE CASCADE",
            "ALTER TABLE sessions ADD COLUMN origin TEXT NOT NULL DEFAULT 'user'",
            "ALTER TABLE schedules ADD COLUMN agent_id TEXT "
            "REFERENCES agents(id) ON DELETE CASCADE",
            "ALTER TABLE bridge_mappings ADD COLUMN agent_id TEXT "
            "REFERENCES agents(id) ON DELETE CASCADE",
        ):
            try:
                await self._conn.execute(ddl)
            except Exception:
                pass

        # 2. The protected Default Agent — exactly one, created once.
        cursor = await self._conn.execute(
            "SELECT id FROM agents WHERE is_system = 1 LIMIT 1"
        )
        row = await cursor.fetchone()
        if row is None:
            default_id = uuid.uuid4().hex[:12]
            now = datetime.now(timezone.utc).isoformat()
            await self._conn.execute(
                "INSERT INTO agents "
                "(id, name, description, system_prompt, mcp_servers, "
                " is_system, created_at, updated_at) "
                "VALUES (?, 'Octo', '', '', ?, 1, ?, ?)",
                (default_id, _DEFAULT_MCP_SERVERS_JSON, now, now),
            )
        else:
            default_id = row[0]
            # One-time rename of the auto-created system agent from its old
            # 'Default' name to 'Octo'. Guarded on the exact old name so a
            # user-renamed system agent is left alone; try/except so it no-ops
            # if an agent named 'Octo' already exists (unique-name index).
            try:
                await self._conn.execute(
                    "UPDATE agents SET name = 'Octo' "
                    "WHERE id = ? AND name = 'Default'",
                    (default_id,),
                )
            except Exception:
                pass

        # 3. Backfill sessions → Default Agent. (origin defaults to 'user'.)
        await self._conn.execute(
            "UPDATE sessions SET agent_id = ? WHERE agent_id IS NULL",
            (default_id,),
        )

        # 4. Schedules: derive agent_id through the (about-to-be-removed)
        #    session_id, then rebuild the table without it. Guarded on the
        #    presence of session_id so it runs exactly once.
        if await self._has_column("schedules", "session_id"):
            await self._conn.execute(
                "UPDATE schedules SET agent_id = ("
                "  SELECT s.agent_id FROM sessions s WHERE s.id = schedules.session_id"
                ") WHERE agent_id IS NULL"
            )
            # Orphans whose session was deleted fall back to Default.
            await self._conn.execute(
                "UPDATE schedules SET agent_id = ? WHERE agent_id IS NULL",
                (default_id,),
            )
            await self._conn.executescript(
                """
                CREATE TABLE schedules__new (
                    id TEXT PRIMARY KEY,
                    agent_id TEXT REFERENCES agents(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    interval_seconds INTEGER NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    last_run_at TEXT
                );
                INSERT INTO schedules__new
                    (id, agent_id, name, prompt, interval_seconds, enabled,
                     created_at, last_run_at)
                    SELECT id, agent_id, name, prompt, interval_seconds, enabled,
                           created_at, last_run_at FROM schedules;
                DROP TABLE schedules;
                ALTER TABLE schedules__new RENAME TO schedules;
                """
            )

        # 5. Bridge mappings: derive agent_id, then rebuild to relax
        #    session_id's NOT NULL into a nullable sticky pointer. Guarded
        #    on the old NOT NULL shape so it runs exactly once.
        if await self._column_is_not_null("bridge_mappings", "session_id"):
            await self._conn.execute(
                "UPDATE bridge_mappings SET agent_id = ("
                "  SELECT s.agent_id FROM sessions s "
                "  WHERE s.id = bridge_mappings.session_id"
                ") WHERE agent_id IS NULL"
            )
            await self._conn.execute(
                "UPDATE bridge_mappings SET agent_id = ? WHERE agent_id IS NULL",
                (default_id,),
            )
            await self._conn.executescript(
                """
                CREATE TABLE bridge_mappings__new (
                    platform TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    agent_id TEXT REFERENCES agents(id) ON DELETE CASCADE,
                    session_id TEXT,
                    PRIMARY KEY (platform, chat_id),
                    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE SET NULL
                );
                INSERT INTO bridge_mappings__new (platform, chat_id, agent_id, session_id)
                    SELECT platform, chat_id, agent_id, session_id FROM bridge_mappings;
                DROP TABLE bridge_mappings;
                ALTER TABLE bridge_mappings__new RENAME TO bridge_mappings;
                """
            )

    async def _ensure_connected(self) -> None:
        # A closed Database is dead — never silently re-open. The
        # previous "reconnect" path was load-bearing for nothing in
        # production and was the root cause of a pytest atexit hang:
        # tests that closed the DB still had pending consumer tasks
        # that would call flush() during loop teardown, the reconnect
        # spawned a brand-new aiosqlite worker thread right before
        # the loop died, and that orphaned non-daemon thread pinned
        # the process. We raise CancelledError so in-flight callers
        # (e.g. session_manager._consume_message) exit cleanly via
        # their existing CancelledError handling.
        if self._closed:
            raise asyncio.CancelledError("Database is closed")
        assert self._conn is not None, "Database not initialized"

    async def close(self) -> None:
        if self._conn:
            if self._dirty:
                await self._conn.commit()
                self._dirty = False
            await self._conn.close()
            self._conn = None
        self._closed = True

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
        agent_id: str | None = None,
        origin: str = "user",
        backend: str = "claude-code",
    ) -> None:
        await self._ensure_connected()
        await self._conn.execute(
            "INSERT INTO sessions "
            "(id, name, working_dir, created_at, claude_session_id, "
            " credential_id, agent_id, origin, backend) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session_id,
                name,
                working_dir,
                created_at,
                claude_session_id,
                credential_id,
                agent_id,
                origin,
                backend,
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
            "credential_id, archived, agent_id, origin, backend FROM sessions"
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
                "agent_id": row[7],
                "origin": row[8] or "user",
                "backend": row[9] or "claude-code",
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
        attachments: list[dict[str, Any]] | None = None,
    ) -> None:
        await self._ensure_connected()
        content_str = json.dumps(content) if content is not None else None
        tool_input_str = json.dumps(tool_input) if tool_input is not None else None
        is_error_int = int(is_error) if is_error is not None else None
        attachments_str = (
            json.dumps(attachments) if attachments else None
        )

        await self._conn.execute(
            "INSERT INTO messages "
            "(session_id, seq, role, type, content, tool_name, tool_input, "
            "tool_use_id, is_error, session_id_ref, cost, attachments) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                attachments_str,
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
            "is_error, session_id_ref, cost, attachments "
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
            attachments = json.loads(row[10]) if row[10] is not None else []
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
                    "attachments": attachments,
                }
            )
        return results

    # --- Bridge mappings ---

    async def save_bridge_mapping(
        self,
        platform: str,
        chat_id: str,
        agent_id: str,
        session_id: str | None = None,
    ) -> None:
        """Bind (platform, chat_id) to an agent, with an optional sticky
        session pointer (the currently-open thread for this chat)."""
        await self._ensure_connected()
        await self._conn.execute(
            "INSERT OR REPLACE INTO bridge_mappings "
            "(platform, chat_id, agent_id, session_id) VALUES (?, ?, ?, ?)",
            (platform, chat_id, agent_id, session_id),
        )
        await self._conn.commit()

    async def set_bridge_sticky_session(
        self, platform: str, chat_id: str, session_id: str | None
    ) -> None:
        """Repoint a chat's sticky session (or clear it with None) without
        touching its agent binding."""
        await self._ensure_connected()
        await self._conn.execute(
            "UPDATE bridge_mappings SET session_id = ? "
            "WHERE platform = ? AND chat_id = ?",
            (session_id, platform, chat_id),
        )
        await self._conn.commit()

    async def clear_bridge_sticky_for_session(self, session_id: str) -> int:
        """Null every sticky pointer aimed at a session that's going away
        (archived). The chat keeps its agent binding; the next inbound
        message opens a fresh thread. Returns rows updated."""
        await self._ensure_connected()
        cursor = await self._conn.execute(
            "UPDATE bridge_mappings SET session_id = NULL WHERE session_id = ?",
            (session_id,),
        )
        await self._conn.commit()
        return cursor.rowcount

    async def delete_bridge_mapping(self, platform: str, chat_id: str) -> None:
        await self._ensure_connected()
        await self._conn.execute(
            "DELETE FROM bridge_mappings WHERE platform = ? AND chat_id = ?",
            (platform, chat_id),
        )
        await self._conn.commit()

    async def load_bridge_mappings(self) -> list[dict[str, str | None]]:
        await self._ensure_connected()
        cursor = await self._conn.execute(
            "SELECT platform, chat_id, agent_id, session_id FROM bridge_mappings"
        )
        rows = await cursor.fetchall()
        return [
            {
                "platform": row[0],
                "chat_id": row[1],
                "agent_id": row[2],
                "session_id": row[3],
            }
            for row in rows
        ]

    # --- Schedules ---

    async def save_schedule(
        self,
        schedule_id: str,
        agent_id: str,
        name: str,
        prompt: str,
        interval_seconds: int,
        created_at: str,
        enabled: bool = True,
    ) -> None:
        await self._ensure_connected()
        await self._conn.execute(
            "INSERT INTO schedules (id, agent_id, name, prompt, interval_seconds, enabled, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (schedule_id, agent_id, name, prompt, interval_seconds, int(enabled), created_at),
        )
        await self._conn.commit()

    async def load_schedules(self) -> list[dict[str, Any]]:
        await self._ensure_connected()
        cursor = await self._conn.execute(
            "SELECT id, agent_id, name, prompt, interval_seconds, enabled, created_at, last_run_at "
            "FROM schedules"
        )
        rows = await cursor.fetchall()
        return [
            {
                "id": row[0],
                "agent_id": row[1],
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
            "agent_id",
            "origin",
            "backend",
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

    # --- Agents ---

    # Agents own sessions, schedules and bridge bindings (agent-refactor.md
    # §4.1). Stateless rows — AgentManager wraps these for the routes;
    # SessionManager reads them directly at spawn time so editing an agent
    # affects its open sessions on their next turn.

    _AGENT_COLS = (
        "id, name, description, avatar, system_prompt, model, credential_id, "
        "mcp_servers, tool_allow, tool_deny, is_system, archived, "
        "created_at, updated_at"
    )

    @staticmethod
    def _row_to_agent(row: tuple[Any, ...]) -> dict[str, Any]:
        try:
            mcp_servers = json.loads(row[7]) if row[7] else []
        except (json.JSONDecodeError, TypeError):
            mcp_servers = []
        agent = {
            "id": row[0],
            "name": row[1],
            "description": row[2] or "",
            "avatar": row[3],
            "system_prompt": row[4] or "",
            "model": row[5],
            "credential_id": row[6],
            "mcp_servers": mcp_servers,
            "tool_allow": row[8] or "",
            "tool_deny": row[9] or "",
            "is_system": bool(row[10]),
            "archived": bool(row[11]),
            "created_at": row[12],
            "updated_at": row[13],
        }
        # Optional active-session count appended by load_agents / get_agent.
        if len(row) > 14:
            agent["active_session_count"] = row[14]
        return agent

    # Subquery counting live (non-archived) sessions for an agent — shared
    # by load_agents and get_agent so the UI can show "3 sessions".
    _ACTIVE_SESSION_COUNT = (
        "(SELECT COUNT(*) FROM sessions s "
        " WHERE s.agent_id = a.id AND s.archived = 0)"
    )

    async def save_agent(
        self,
        *,
        agent_id: str,
        name: str,
        created_at: str,
        updated_at: str,
        description: str = "",
        avatar: str | None = None,
        system_prompt: str = "",
        model: str | None = None,
        credential_id: str | None = None,
        mcp_servers: list[str] | None = None,
        tool_allow: str = "",
        tool_deny: str = "",
        is_system: bool = False,
    ) -> None:
        await self._ensure_connected()
        servers_json = json.dumps(
            mcp_servers if mcp_servers is not None else _DEFAULT_MCP_SERVERS
        )
        await self._conn.execute(
            "INSERT INTO agents "
            "(id, name, description, avatar, system_prompt, model, "
            " credential_id, mcp_servers, tool_allow, tool_deny, is_system, "
            " archived, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
            (
                agent_id, name, description, avatar, system_prompt, model,
                credential_id, servers_json, tool_allow, tool_deny,
                int(bool(is_system)), created_at, updated_at,
            ),
        )
        await self._conn.commit()

    async def load_agents(
        self, *, include_archived: bool = False
    ) -> list[dict[str, Any]]:
        await self._ensure_connected()
        cols = ", ".join(f"a.{c}" for c in self._AGENT_COLS.split(", "))
        query = (
            f"SELECT {cols}, {self._ACTIVE_SESSION_COUNT} FROM agents a"
        )
        if not include_archived:
            query += " WHERE a.archived = 0"
        query += " ORDER BY a.is_system DESC, a.created_at"
        cursor = await self._conn.execute(query)
        rows = await cursor.fetchall()
        return [self._row_to_agent(row) for row in rows]

    async def get_agent(self, agent_id: str) -> dict[str, Any] | None:
        await self._ensure_connected()
        cols = ", ".join(f"a.{c}" for c in self._AGENT_COLS.split(", "))
        cursor = await self._conn.execute(
            f"SELECT {cols}, {self._ACTIVE_SESSION_COUNT} FROM agents a "
            "WHERE a.id = ?",
            (agent_id,),
        )
        row = await cursor.fetchone()
        return self._row_to_agent(row) if row else None

    async def get_agent_by_name(
        self, name: str, *, include_archived: bool = False
    ) -> dict[str, Any] | None:
        await self._ensure_connected()
        cols = ", ".join(f"a.{c}" for c in self._AGENT_COLS.split(", "))
        query = (
            f"SELECT {cols}, {self._ACTIVE_SESSION_COUNT} FROM agents a "
            "WHERE a.name = ?"
        )
        params: list[Any] = [name]
        if not include_archived:
            query += " AND a.archived = 0"
        cursor = await self._conn.execute(query, params)
        row = await cursor.fetchone()
        return self._row_to_agent(row) if row else None

    async def get_system_agent(self) -> dict[str, Any] | None:
        """The protected Default Agent (is_system=1), created by migration."""
        await self._ensure_connected()
        cols = ", ".join(f"a.{c}" for c in self._AGENT_COLS.split(", "))
        cursor = await self._conn.execute(
            f"SELECT {cols}, {self._ACTIVE_SESSION_COUNT} FROM agents a "
            "WHERE a.is_system = 1 LIMIT 1"
        )
        row = await cursor.fetchone()
        return self._row_to_agent(row) if row else None

    async def update_agent(self, agent_id: str, **fields: Any) -> None:
        await self._ensure_connected()
        allowed = {
            "name", "description", "avatar", "system_prompt", "model",
            "credential_id", "mcp_servers", "tool_allow", "tool_deny",
            "archived",
        }
        # credential_id / model / avatar are nullable and may be cleared.
        nullable = {"credential_id", "model", "avatar"}
        updates: dict[str, Any] = {}
        for k, v in fields.items():
            if k not in allowed:
                continue
            if v is None and k not in nullable:
                continue
            if k == "mcp_servers":
                updates[k] = json.dumps(v if v is not None else [])
            elif k == "archived":
                updates[k] = int(bool(v))
            else:
                updates[k] = v
        if not updates:
            return
        updates["updated_at"] = datetime.now(timezone.utc).isoformat()
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [agent_id]
        await self._conn.execute(
            f"UPDATE agents SET {set_clause} WHERE id = ?", values
        )
        await self._conn.commit()

    async def count_active_sessions_for_agent(self, agent_id: str) -> int:
        await self._ensure_connected()
        cursor = await self._conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE agent_id = ? AND archived = 0",
            (agent_id,),
        )
        row = await cursor.fetchone()
        return row[0]

    async def count_sessions_for_agent(self, agent_id: str) -> int:
        await self._ensure_connected()
        cursor = await self._conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE agent_id = ?",
            (agent_id,),
        )
        row = await cursor.fetchone()
        return row[0]

    async def archive_agent(self, agent_id: str) -> None:
        """Soft-delete an agent and cascade-archive its sessions."""
        await self._ensure_connected()
        await self._conn.execute(
            "UPDATE agents SET archived = 1, updated_at = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), agent_id),
        )
        await self._conn.execute(
            "UPDATE sessions SET archived = 1 WHERE agent_id = ?",
            (agent_id,),
        )
        await self._conn.commit()

    async def delete_agent(self, agent_id: str) -> bool:
        """Hard-delete an agent. FK ON DELETE CASCADE removes its sessions,
        schedules and bridge bindings — guarded by AgentManager so this is
        only reached when the agent has no sessions."""
        await self._ensure_connected()
        cursor = await self._conn.execute(
            "DELETE FROM agents WHERE id = ?", (agent_id,)
        )
        await self._conn.commit()
        return cursor.rowcount > 0

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

    # --- Background tasks (cross-turn) ---

    @staticmethod
    def _row_to_bg_task(row: tuple[Any, ...]) -> dict[str, Any]:
        return {
            "id": row[0],
            "session_id": row[1],
            "command": row[2],
            "description": row[3],
            "working_dir": row[4],
            "status": row[5],
            "exit_code": row[6],
            "stdout": row[7] or "",
            "stderr": row[8] or "",
            "truncated": bool(row[9]),
            "started_at": row[10],
            "completed_at": row[11],
        }

    _BG_TASK_COLS = (
        "id, session_id, command, description, working_dir, status, "
        "exit_code, stdout, stderr, truncated, started_at, completed_at"
    )

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
        await self._conn.execute(
            "INSERT INTO bg_tasks "
            "(id, session_id, command, description, working_dir, status, "
            " stdout, stderr, truncated, started_at) "
            "VALUES (?, ?, ?, ?, ?, 'running', '', '', 0, ?)",
            (task_id, session_id, command, description, working_dir, started_at),
        )
        await self._conn.commit()

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
        await self._conn.execute(
            f"UPDATE bg_tasks SET {set_clause} WHERE id = ?", values
        )
        await self._conn.commit()

    async def get_bg_task(self, task_id: str) -> dict[str, Any] | None:
        await self._ensure_connected()
        cursor = await self._conn.execute(
            f"SELECT {self._BG_TASK_COLS} FROM bg_tasks WHERE id = ?", (task_id,)
        )
        row = await cursor.fetchone()
        return self._row_to_bg_task(row) if row else None

    async def list_bg_tasks_for_session(
        self, session_id: str, *, limit: int = 200
    ) -> list[dict[str, Any]]:
        await self._ensure_connected()
        cursor = await self._conn.execute(
            f"SELECT {self._BG_TASK_COLS} FROM bg_tasks "
            "WHERE session_id = ? ORDER BY started_at DESC LIMIT ?",
            (session_id, limit),
        )
        rows = await cursor.fetchall()
        return [self._row_to_bg_task(r) for r in rows]

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
        cursor = await self._conn.execute(
            "UPDATE bg_tasks SET status = 'interrupted', completed_at = ? "
            "WHERE status IN ('running', 'pending')",
            (completed_at,),
        )
        await self._conn.commit()
        return cursor.rowcount
