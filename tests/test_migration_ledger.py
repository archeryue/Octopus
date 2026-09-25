"""The migration ledger, and the swallow it replaced.

`_apply_migrations` used to be 200 lines of `try: ALTER ...; except Exception:
pass`. The comment said "column already exists", but a bare handler equally
absorbed a misspelled column, a renamed table, a locked database and a full
disk — so a broken migration silently no-opped and resurfaced much later as an
inexplicably missing column. A live run of scripts/check.sh caught the
mechanism firing on an ordinary test: `sqlite3.OperationalError: duplicate
column name: delegation_request`, absorbed, visible only because it happened to
escape as a GC-time warning.

These tests pin both halves of the fix: what ran is now a fact in the database,
and a genuine failure is loud.
"""

from __future__ import annotations

import sqlite3

import pytest

from server.database import Database


async def _ledger(db: Database) -> set[str]:
    cur = await db.conn.execute("SELECT version FROM schema_migrations")
    return {r[0] for r in await cur.fetchall()}


@pytest.fixture
async def db():
    database = Database(":memory:")
    await database.initialize()
    yield database
    await database.close()


class TestLedger:
    async def test_every_migration_is_recorded(self, db):
        versions = await _ledger(db)
        # One per declared column migration, plus the named index/backfill steps.
        expected = {
            f"column:{t}.{c}"
            for t, c, _ in (*Database._COLUMN_MIGRATIONS, *Database._LATE_COLUMN_MIGRATIONS)
        }
        assert expected <= versions
        assert "index:idx_sessions_forked_from" in versions
        assert "backfill:builtin_mcp_servers_v3" in versions

    async def test_a_fresh_database_stamps_without_altering(self, db):
        """On a fresh DB every column already exists (CREATE TABLE made them),
        so each step verifies-and-stamps rather than running DDL. The ledger
        must still be complete — it records what is true, not what it did."""
        versions = await _ledger(db)
        assert "column:sessions.credential_id" in versions
        assert "column:messages.git_head" in versions

    async def test_second_run_is_idempotent(self, db):
        before = await _ledger(db)
        await db._apply_migrations()
        assert await _ledger(db) == before


class TestFailuresAreLoud:
    """The point of the change: a migration that cannot apply must raise."""

    async def test_a_typo_between_name_and_ddl_raises(self, db):
        """The realistic bug the old handler hid: the declared column and the
        DDL disagree. The guard sees the declared name missing, runs the DDL,
        and the DDL fails on the name it actually targets. Under
        `except Exception: pass` this silently no-opped and the intended column
        was simply absent forever."""
        with pytest.raises(sqlite3.OperationalError):
            await db._add_column(
                "sessions", "intended_name",
                "ALTER TABLE sessions ADD COLUMN archived INTEGER",  # already exists
            )

    async def test_malformed_ddl_raises(self, db):
        with pytest.raises(sqlite3.OperationalError):
            await db._add_column(
                "sessions", "another_new_col", "ALTER TABLE sessions ADD COLUMN"
            )

    async def test_a_missing_table_raises(self, db):
        with pytest.raises(sqlite3.OperationalError):
            await db._add_column(
                "no_such_table", "c", "ALTER TABLE no_such_table ADD COLUMN c TEXT"
            )

    async def test_a_real_column_is_added_and_stamped(self, db):
        assert not await db._has_column("sessions", "probe_col")
        await db._add_column(
            "sessions", "probe_col", "ALTER TABLE sessions ADD COLUMN probe_col TEXT"
        )
        assert await db._has_column("sessions", "probe_col")
        assert "column:sessions.probe_col" in await _ledger(db)

    async def test_an_existing_column_is_stamped_not_re_added(self, db):
        """A database that predates the ledger has the columns but no record of
        them. The column is the evidence, so it is stamped — and stamping must
        not attempt the DDL again, which would raise duplicate-column."""
        await db.conn.execute("ALTER TABLE sessions ADD COLUMN legacy_col TEXT")
        await db._add_column(
            "sessions", "legacy_col", "ALTER TABLE sessions ADD COLUMN legacy_col TEXT"
        )
        assert "column:sessions.legacy_col" in await _ledger(db)
