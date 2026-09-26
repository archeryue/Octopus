from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
import uuid
from datetime import UTC, datetime
from typing import Any

import aiosqlite

from ..monitor import Event as _MonEvent
from ..monitor import record as _mon_record
from .schema import _DEFAULT_MCP_SERVERS_JSON, _SCHEMA

logger = logging.getLogger(__name__)

# Bounds on the deferred commit (B4). Whichever trips first flushes, so the
# worst case is ~half a second or ~32 messages of transcript at risk from a
# hard kill, instead of an entire turn.
_FLUSH_AFTER_SECONDS = 0.5
_FLUSH_EVERY_APPENDS = 32

# Built-in MCP servers attached to the Default Agent (and the default for
# any newly-created agent). Kept here so the migration backfill and the
# CREATE TABLE default stay in lock-step.







def _load_json_list(raw: Any) -> list[Any]:
    """A JSON-list column as a list, whatever shape the column is in.

    Columns like `agents.subagents` are additive: rows that predate them read
    back NULL, and a hand-edited row can hold anything. A decode failure
    degrades to "none defined" rather than breaking the agent.
    """
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    return value if isinstance(value, list) else []

class DatabaseBase:
    """Connection, transactions and migrations — everything the domain
    mixins stand on.

    Separate from them so the lifecycle has one home: opening the file, the
    pragmas, the deferred-commit bound, the migration ledger. A mixin inherits
    this rather than declaring the attributes it borrows, which keeps the type
    checker honest about what it may touch.
    """

    # Row mappers for the tables more than one domain reads. Each sits beside
    # the column list it consumes — they are one unit, and separating them is
    # how a column gets added to one and not the other.

    @staticmethod
    def _row_to_application(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row[0],
            "name": row[1],
            "description": row[2],
            "icon": row[3],
            "icon_src": row[4],
            "agent_id": row[5],
            "session_id": row[6],
            "app_dir": row[7],
            "entrypoint": row[8],
            "status": row[9],
            "error": row[10],
            "archived": bool(row[11]),
            "created_at": row[12],
            "updated_at": row[13],
            "last_built_at": row[14],
        }
    @staticmethod
    def _row_to_bg_task(row: sqlite3.Row) -> dict[str, Any]:
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
    @staticmethod
    def _row_to_connector(row: sqlite3.Row) -> dict[str, Any]:
        try:
            scopes = json.loads(row[5]) if row[5] else []
        except (json.JSONDecodeError, TypeError):
            scopes = []
        return {
            "id": row[0],
            "kind": row[1],
            "label": row[2],
            "auth_type": row[3],
            "external_account_id": row[4],
            "scopes": scopes,
            "enable_by_default": bool(row[6]),
            "needs_reconnect": bool(row[7]),
            "token_expires_at": row[8],
            "last_refresh_error_code": row[9],
            "created_at": row[10],
        }
    @staticmethod
    def _row_to_research_job(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row[0],
            "session_id": row[1],
            "question": row[2],
            "status": row[3],
            "phase": row[4],
            "error": row[5],
            "report_path": row[6],
            "cost": row[7],
            "created_at": row[8],
            "completed_at": row[9],
            "injection_status": row[10],
            "injected_at": row[11],
        }

    # Column lists and canned SQL shared by the domain mixins.
    #
    # They live on the base rather than each mixin because several genuinely
    # cross domains: deleting a session has to clear its background tasks,
    # research jobs and application threads, so `sessions` needs those column
    # lists too. Putting them here states that overlap once instead of
    # duplicating three tuples, and every mixin inherits this class anyway.
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
    _CONNECTOR_COLS = (
        "id, kind, label, auth_type, external_account_id, scopes, "
        "enable_by_default, needs_reconnect, token_expires_at, "
        "last_refresh_error_code, created_at"
    )
    _CUSTOM_COLS = (
        "kind, display_name, authorize_url, token_url, scopes, pkce, "
        "api_base, created_at, updated_at"
    )
    _AGENT_COLS = (
        "id, name, description, avatar, system_prompt, model, credential_id, "
        "mcp_servers, tool_allow, tool_deny, is_system, archived, "
        "created_at, updated_at, backend, subagents"
    )
    # Subquery counting live (non-archived) sessions for an agent — shared
    # by load_agents and get_agent so the UI can show "3 sessions".
    _ACTIVE_SESSION_COUNT = (
        "(SELECT COUNT(*) FROM sessions s "
        " WHERE s.agent_id = a.id AND s.archived = 0)"
    )
    _BG_TASK_COLS = (
        "id, session_id, command, description, working_dir, status, "
        "exit_code, stdout, stderr, truncated, started_at, completed_at"
    )
    _RESEARCH_COLS = (
        "id, session_id, question, status, phase, error, report_path, cost, "
        "created_at, completed_at, injection_status, injected_at"
    )
    _APPLICATION_COLS = (
        "id, name, description, icon, icon_src, agent_id, session_id, "
        "app_dir, entrypoint, status, error, archived, created_at, "
        "updated_at, last_built_at"
    )

    # Additive column migrations, as (table, column, DDL). Declared as data so
    # the list reads as an inventory and every entry goes through the same
    # guarded path. Order matters only where a later rebuild would drop a
    # `schedules` columns are applied after the schedule migrations, which
    # rebuild that table.
    _COLUMN_MIGRATIONS: tuple[tuple[str, str, str], ...] = (
        # Per-backend auth.
        ("sessions", "credential_id", "ALTER TABLE sessions ADD COLUMN credential_id TEXT"),
        # /archive — hides the row from the default list, keeps it in the DB.
        ("sessions", "archived",
         "ALTER TABLE sessions ADD COLUMN archived INTEGER NOT NULL DEFAULT 0"),
        # Credential status / refresh tracking (B-4/B-5).
        ("backend_credentials", "status",
         "ALTER TABLE backend_credentials ADD COLUMN status TEXT NOT NULL DEFAULT 'active'"),
        ("backend_credentials", "token_expires_at",
         "ALTER TABLE backend_credentials ADD COLUMN token_expires_at TEXT"),
        ("backend_credentials", "needs_reconnect",
         "ALTER TABLE backend_credentials ADD COLUMN needs_reconnect INTEGER NOT NULL DEFAULT 0"),
        ("backend_credentials", "last_refresh_error_code",
         "ALTER TABLE backend_credentials ADD COLUMN last_refresh_error_code TEXT"),
        # File/image upload.
        ("messages", "attachments", "ALTER TABLE messages ADD COLUMN attachments TEXT"),
        # codex-backend.md §4.1 — DEFAULT backfills existing rows, no behaviour change.
        ("sessions", "backend",
         "ALTER TABLE sessions ADD COLUMN backend TEXT NOT NULL DEFAULT 'claude-code'"),
        # Application archive/restore.
        ("applications", "archived",
         "ALTER TABLE applications ADD COLUMN archived INTEGER NOT NULL DEFAULT 0"),
        # The app's own icon, re-evaluated on each build; NULL until then.
        ("applications", "icon_src", "ALTER TABLE applications ADD COLUMN icon_src TEXT"),
    )

    # Applied after `_migrate_agents` / the schedule migrations, because those
    # rebuild `schedules` and a column added earlier would not survive the
    # rebuild.
    _LATE_COLUMN_MIGRATIONS: tuple[tuple[str, str, str], ...] = (
        # Default harness for an agent's new sessions.
        ("agents", "backend",
         "ALTER TABLE agents ADD COLUMN backend TEXT NOT NULL DEFAULT 'claude-code'"),
        # agent-collaboration.md §4.1 — a delegation child points at its parent
        # (SET NULL on parent delete: orphaning beats mass-delete) and carries
        # the original request for the UI header. `origin` gains 'delegation',
        # which is a caller change, not DDL.
        ("sessions", "parent_session_id",
         "ALTER TABLE sessions ADD COLUMN parent_session_id TEXT "
         "REFERENCES sessions(id) ON DELETE SET NULL"),
        ("sessions", "delegation_request",
         "ALTER TABLE sessions ADD COLUMN delegation_request TEXT"),
        # native-subagents.md §6 — '[]' means "the CLI's built-ins only".
        ("agents", "subagents",
         "ALTER TABLE agents ADD COLUMN subagents TEXT NOT NULL DEFAULT '[]'"),
        # app-agent-access.md §3 — names the owning application; `origin` gains
        # 'app' as a caller change.
        ("sessions", "app_id", "ALTER TABLE sessions ADD COLUMN app_id TEXT"),
        # session-rewind.md §4 — six nullable columns on sessions, two on
        # messages. forked_from deliberately has no FK action, so a dangling
        # reference survives a parent delete.
        ("sessions", "forked_from_session_id",
         "ALTER TABLE sessions ADD COLUMN forked_from_session_id TEXT"),
        ("sessions", "fork_after_seq", "ALTER TABLE sessions ADD COLUMN fork_after_seq INTEGER"),
        ("sessions", "fork_needs_replay",
         "ALTER TABLE sessions ADD COLUMN fork_needs_replay INTEGER NOT NULL DEFAULT 0"),
        ("sessions", "fork_metadata", "ALTER TABLE sessions ADD COLUMN fork_metadata TEXT"),
        ("sessions", "fork_revert_record",
         "ALTER TABLE sessions ADD COLUMN fork_revert_record TEXT"),
        ("sessions", "fork_status", "ALTER TABLE sessions ADD COLUMN fork_status TEXT"),
        ("messages", "git_head", "ALTER TABLE messages ADD COLUMN git_head TEXT"),
        ("messages", "git_status_clean",
         "ALTER TABLE messages ADD COLUMN git_status_clean INTEGER"),
    )


    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._conn: aiosqlite.Connection | None = None
        self._dirty: bool = False
        self._closed: bool = False
        self._pending_appends: int = 0
        self._last_flush: float = time.monotonic()

    async def initialize(self) -> None:
        self._conn = await aiosqlite.connect(self._db_path)
        await self.conn.execute("PRAGMA journal_mode=WAL")
        await self.conn.execute("PRAGMA foreign_keys=ON")
        # Wait rather than fail if another connection holds the write lock —
        # the CLI's own tooling and the test suite both open this file, and the
        # default is to raise "database is locked" immediately (B5).
        await self.conn.execute("PRAGMA busy_timeout=5000")
        # NORMAL is the standard pairing with WAL: a commit does not fsync, so
        # a crash can lose the last commits but cannot corrupt the database.
        # Stated explicitly because the durability story below (the timed
        # flush) depends on knowing which it is.
        await self.conn.execute("PRAGMA synchronous=NORMAL")
        await self.conn.executescript(_SCHEMA)
        await self._apply_migrations()
        await self.conn.commit()

    async def _stamp(self, version: str) -> None:
        """Record a migration as applied. The ledger exists so "what has run"
        is a fact in the database rather than an inference from the schema."""
        await self.conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (version, datetime.now(UTC).isoformat()),
        )

    async def _add_column(self, table: str, column: str, ddl: str) -> None:
        """Apply one additive column migration, guarded by introspection.

        This replaces `try: ALTER ...; except Exception: pass`. SQLite has no
        `ADD COLUMN IF NOT EXISTS`, and the old form papered over that by
        swallowing *every* exception — a misspelled column, a renamed table, a
        locked database and a full disk all looked identical to "already
        applied", so a broken migration silently no-opped and resurfaced much
        later as an inexplicably missing column. Asking `PRAGMA table_info`
        first means the expected case is tested for, and anything else raises.

        Stamping after a column is found to *already* exist is deliberate: on a
        database that predates the ledger the column is the evidence, so we
        record what we verified rather than assuming a clean history.
        """
        if await self._has_column(table, column):
            await self._stamp(f"column:{table}.{column}")
            return
        await self.conn.execute(ddl)
        await self._stamp(f"column:{table}.{column}")
        logger.info("migration applied: %s.%s", table, column)

    async def _apply_migrations(self) -> None:
        """Bring an existing database up to the current schema.

        Idempotent and safe on every boot. Every step either verifies it has
        already been applied or applies it; nothing is inferred from a
        swallowed exception, so a genuine DDL failure now propagates instead of
        masquerading as "already done".
        """
        for table, column, ddl in self._COLUMN_MIGRATIONS:
            await self._add_column(table, column, ddl)

        # Storage split (B-4): copy pre-split secrets into their own table.
        # New writes go there directly; this is a one-off catch-up, and
        # INSERT OR IGNORE makes re-running a no-op. Kept in a try because it
        # reads a legacy column that fresh databases do not have at all — the
        # one place where "the statement may be invalid here" is the expected
        # case rather than a bug, so it logs instead of passing silently.
        try:
            await self.conn.execute(
                "INSERT OR IGNORE INTO credential_secrets "
                "(credential_id, secret_encrypted) "
                "SELECT id, secret_encrypted FROM backend_credentials"
            )
            await self._stamp("backfill:credential_secrets")
        except Exception:
            logger.exception("credential storage-split backfill failed")

        # The applications name index was originally unconditional, which
        # reserved a name forever: archiving an app then blocked reusing it.
        # Rebuild it live-only (the same rule agents use). Guarded on the
        # index's own SQL, so it runs exactly once.
        cur = await self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'index' "
            "AND name = 'applications_name_unique'"
        )
        row = await cur.fetchone()
        if row and row[0] and "archived" not in row[0]:
            await self.conn.execute("DROP INDEX applications_name_unique")
            await self.conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS applications_name_unique"
                " ON applications(name COLLATE NOCASE) WHERE archived = 0"
            )
            logger.info("migration applied: applications_name_unique -> live-only")
        await self._stamp("index:applications_name_unique_live_only")

        await self._migrate_agents()
        await self._migrate_schedule_recurrence()
        await self._migrate_schedule_run_at()

        for table, column, ddl in self._LATE_COLUMN_MIGRATIONS:
            await self._add_column(table, column, ddl)

        await self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sessions_forked_from "
            "ON sessions(forked_from_session_id)"
        )
        await self._stamp("index:idx_sessions_forked_from")

        # Backfill default built-in MCP servers into every existing agent as new
        # ones land (`ask_agent` — agent-collaboration.md §5.1; `research` —
        # native-deep-research.md §7; `schedule` — schedule-tool.md §4). The
        # CREATE TABLE default only reaches brand-new rows on fresh databases;
        # pre-existing rows already hold a value and need this catch-up.
        # Set-membership makes re-running a no-op.
        await self._backfill_builtin_mcp_servers(
            ("ask_agent", "research", "schedule")
        )
        await self._stamp("backfill:builtin_mcp_servers_v3")

        # The Telegram bridge was removed (2026-09-25). Octopus is usable from a
        # phone browser directly, which is what the bridge existed to provide;
        # the "an agent can be @-mentioned on a platform that has bots" idea
        # that might replace it is a different design, not this table.
        #
        # Dropped rather than left behind: a table nothing reads is a question
        # every future reader has to answer. The rows were chat->agent bindings
        # — routing state, regenerated by re-binding a chat — so there is
        # nothing here a user would miss.
        await self.conn.execute("DROP TABLE IF EXISTS bridge_mappings")
        await self._stamp("drop:bridge_mappings")

    async def _backfill_builtin_mcp_servers(self, names: tuple[str, ...]) -> None:
        cursor = await self.conn.execute("SELECT id, mcp_servers FROM agents")
        rows = list(await cursor.fetchall())
        for agent_id, raw in rows:
            try:
                current = json.loads(raw) if raw else []
            except json.JSONDecodeError:
                # A corrupted blob is left alone — manual rescue from
                # SQLite is safer than guessing what the user meant.
                continue
            if not isinstance(current, list):
                continue
            added = False
            for name in names:
                if name not in current:
                    current.append(name)
                    added = True
            if added:
                await self.conn.execute(
                    "UPDATE agents SET mcp_servers = ? WHERE id = ?",
                    (json.dumps(current), agent_id),
                )

    async def _migrate_schedule_recurrence(self) -> None:
        """Schedules gained cron/timezone/recurrence_label and `interval_seconds`
        became nullable (natural-language + time-of-day scheduling), then later
        an `origin_session_id` (a `/schedule` created in a chat remembers its
        session so fires append into that conversation). Rebuild the table once
        for the recurrence shape — guarded on the `cron` column being absent —
        then additively ensure the origin column. Fresh DBs (already the full
        shape from _SCHEMA) and re-boots no-op. Runs after `_migrate_agents`, so
        the table already has `agent_id` and no `session_id`. Existing rows are
        interval schedules: cron/timezone/recurrence_label stay NULL and the UI
        formats interval_seconds."""
        if not await self._has_column(
            "schedules", "cron"
        ) and await self._has_column("schedules", "interval_seconds"):
            await self.conn.executescript(
                """
                CREATE TABLE schedules__rec (
                    id TEXT PRIMARY KEY,
                    agent_id TEXT REFERENCES agents(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    interval_seconds INTEGER,
                    cron TEXT,
                    timezone TEXT,
                    recurrence_label TEXT,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    last_run_at TEXT
                );
                INSERT INTO schedules__rec
                    (id, agent_id, name, prompt, interval_seconds, enabled,
                     created_at, last_run_at)
                    SELECT id, agent_id, name, prompt, interval_seconds, enabled,
                           created_at, last_run_at FROM schedules;
                DROP TABLE schedules;
                ALTER TABLE schedules__rec RENAME TO schedules;
                """
            )
        # origin_session_id is additive on top of the recurrence shape. Guarded
        # so re-running (and fresh DBs that already have it from _SCHEMA) no-op.
        if not await self._has_column("schedules", "origin_session_id"):
            await self.conn.execute(
                "ALTER TABLE schedules ADD COLUMN origin_session_id TEXT"
            )

    async def _migrate_schedule_run_at(self) -> None:
        """Add run_at column to schedules (one-time schedule support). Fresh DBs
        already have it from _SCHEMA; re-runs are no-ops."""
        if not await self._has_column("schedules", "run_at"):
            await self.conn.execute(
                "ALTER TABLE schedules ADD COLUMN run_at TEXT"
            )
            await self.conn.commit()
        # Which session the last fire ran in. Additive; NULL on every
        # pre-existing row means "we don't know", which both readers handle.
        if not await self._has_column("schedules", "last_run_session_id"):
            await self.conn.execute(
                "ALTER TABLE schedules ADD COLUMN last_run_session_id TEXT"
            )
            await self.conn.commit()

    async def _column_info(self, table: str) -> list[tuple[Any, ...]]:
        cursor = await self.conn.execute(f"PRAGMA table_info({table})")
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

        Adds agent ownership to sessions and schedules,
        creates the protected Default Agent, and backfills every
        pre-existing row to it. Idempotent: safe on every boot, a second
        run no-ops (system agent present, no null agent_id rows, the
        column-shape rebuilds already applied). `schedules.session_id`
        and the schedules table's old shape are removed by
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
        ):
            try:
                await self.conn.execute(ddl)
            except Exception:
                pass

        # 2. The protected Default Agent — exactly one, created once.
        cursor = await self.conn.execute(
            "SELECT id FROM agents WHERE is_system = 1 LIMIT 1"
        )
        row = await cursor.fetchone()
        if row is None:
            default_id = uuid.uuid4().hex[:12]
            now = datetime.now(UTC).isoformat()
            await self.conn.execute(
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
                await self.conn.execute(
                    "UPDATE agents SET name = 'Octo' "
                    "WHERE id = ? AND name = 'Default'",
                    (default_id,),
                )
            except Exception:
                pass

        # 3. Backfill sessions → Default Agent. (origin defaults to 'user'.)
        await self.conn.execute(
            "UPDATE sessions SET agent_id = ? WHERE agent_id IS NULL",
            (default_id,),
        )

        # 4. Schedules: derive agent_id through the (about-to-be-removed)
        #    session_id, then rebuild the table without it. Guarded on the
        #    presence of session_id so it runs exactly once.
        if await self._has_column("schedules", "session_id"):
            await self.conn.execute(
                "UPDATE schedules SET agent_id = ("
                "  SELECT s.agent_id FROM sessions s WHERE s.id = schedules.session_id"
                ") WHERE agent_id IS NULL"
            )
            # Orphans whose session was deleted fall back to Default.
            await self.conn.execute(
                "UPDATE schedules SET agent_id = ? WHERE agent_id IS NULL",
                (default_id,),
            )
            await self.conn.executescript(
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
                await self.conn.commit()
                self._dirty = False
            await self.conn.close()
            self._conn = None
        self._closed = True

    async def flush(self) -> None:
        """Commit pending writes."""
        await self._ensure_connected()
        if self._dirty:
            await self.conn.commit()
            self._dirty = False
            pending, waited = self._pending_appends, time.monotonic() - self._last_flush
            self._pending_appends = 0
            self._last_flush = time.monotonic()
            _mon_record(_MonEvent(
                kind="db_flush",
                duration_ms=waited * 1000,
                ok=True,
                detail={"rows": pending},
            ))

    async def _maybe_flush(self) -> None:
        """Commit if the pending batch has grown old or large enough.

        `append_message` sets `_dirty` and defers the commit, which is the right
        trade for a per-token-ish append path — but it was unbounded. Only three
        call sites flushed explicitly and no timer existed, so a hard kill lost
        everything since the last incidental flush, and a long tool-heavy turn
        with no intervening read lost the most. The production WAL sitting at
        7 MB was this batching visible on disk.

        Whichever bound trips first wins, so the exposure is sub-second in wall
        time and bounded in rows regardless of traffic shape (B4). Note reads
        also flush — `load_messages` calls flush() for write-visibility — so in
        practice this is the floor, not the only trigger.
        """
        if not self._dirty:
            return
        if (
            self._pending_appends >= _FLUSH_EVERY_APPENDS
            or (time.monotonic() - self._last_flush) >= _FLUSH_AFTER_SECONDS
        ):
            await self.flush()

    @property
    def conn(self) -> aiosqlite.Connection:
        assert self._conn is not None, "Database not initialized"
        return self._conn
