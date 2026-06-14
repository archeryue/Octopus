"""Migration/backfill tests for the first-class Agents refactor.

Boots a DB created with the *old* (pre-agents) schema — sessions without
agent_id, schedules with a NOT NULL session_id + FK, bridge_mappings with a
NOT NULL session_id — runs `_apply_migrations()` (twice), and asserts the
ownership graph is rebuilt onto a Default Agent, idempotently. See
docs/plans/agent-refactor.md §4.5.
"""

import sqlite3

import pytest

from server.database import Database

# The schema as it existed before the Agents refactor, for the three tables
# the migration transforms. Everything else is created fresh by Database.
_OLD_SCHEMA = """
CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    working_dir TEXT NOT NULL,
    created_at TEXT NOT NULL,
    claude_session_id TEXT,
    archived INTEGER NOT NULL DEFAULT 0,
    credential_id TEXT
);
CREATE TABLE schedules (
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
CREATE TABLE bridge_mappings (
    platform TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    PRIMARY KEY (platform, chat_id),
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);
"""


def _seed_old_db(path: str) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(_OLD_SCHEMA)
    conn.execute(
        "INSERT INTO sessions (id, name, working_dir, created_at) "
        "VALUES ('s1', 'Old Session', '/tmp', '2025-01-01T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO schedules (id, session_id, name, prompt, interval_seconds, created_at) "
        "VALUES ('sch1', 's1', 'daily', 'do it', 3600, '2025-01-01T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO bridge_mappings (platform, chat_id, session_id) "
        "VALUES ('telegram', 'c1', 's1')"
    )
    conn.commit()
    conn.close()


async def _column_names(db: Database, table: str) -> set[str]:
    cursor = await db.conn.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in await cursor.fetchall()}


@pytest.mark.asyncio
async def test_backfill_from_old_schema(tmp_path):
    db_path = str(tmp_path / "old.db")
    _seed_old_db(db_path)

    db = Database(db_path)
    await db.initialize()  # runs _apply_migrations once
    try:
        # Exactly one protected Default Agent.
        agents = await db.load_agents()
        system = [a for a in agents if a["is_system"]]
        assert len(system) == 1
        default = system[0]
        assert default["name"] == "Octo"
        # The built-in backfill (agent-collaboration.md §5.1 ask_agent;
        # native-deep-research.md §7 research) runs alongside the other
        # migrations and appends to every existing agent's mcp_servers list.
        assert default["mcp_servers"] == ["ask", "bg", "ask_agent", "research"]

        # Session backfilled onto it, origin defaults to 'user', backend to
        # claude-code (codex-backend.md §4.1 migration).
        sessions = await db.load_sessions()
        assert len(sessions) == 1
        assert sessions[0]["agent_id"] == default["id"]
        assert sessions[0]["origin"] == "user"
        assert sessions[0]["backend"] == "claude-code"

        # Schedule re-owned by the agent; session_id column gone.
        schedules = await db.load_schedules()
        assert len(schedules) == 1
        assert schedules[0]["agent_id"] == default["id"]
        assert "session_id" not in await _column_names(db, "schedules")

        # Bridge mapping bound to the agent; session_id preserved as the
        # sticky pointer and is now nullable.
        mappings = await db.load_bridge_mappings()
        assert len(mappings) == 1
        assert mappings[0]["agent_id"] == default["id"]
        assert mappings[0]["session_id"] == "s1"
        assert not await db._column_is_not_null("bridge_mappings", "session_id")

        # Idempotency: a second migration run changes nothing.
        await db._apply_migrations()
        agents2 = await db.load_agents()
        assert len([a for a in agents2 if a["is_system"]]) == 1
        assert (await db.get_agent(default["id"]))["id"] == default["id"]
        assert len(await db.load_schedules()) == 1
        assert len(await db.load_bridge_mappings()) == 1
        sessions2 = await db.load_sessions()
        assert all(s["agent_id"] == default["id"] for s in sessions2)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_fresh_db_gets_default_agent(tmp_path):
    """A brand-new DB is born with the new shape and one Default Agent."""
    db = Database(str(tmp_path / "fresh.db"))
    await db.initialize()
    try:
        system = await db.get_system_agent()
        assert system is not None
        assert system["name"] == "Octo"
        # No session_id leftover on the freshly-created tables.
        assert "session_id" not in await _column_names(db, "schedules")
        assert not await db._column_is_not_null("bridge_mappings", "session_id")

        # Second run no-ops (still exactly one system agent).
        await db._apply_migrations()
        agents = await db.load_agents(include_archived=True)
        assert len([a for a in agents if a["is_system"]]) == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_orphan_schedule_falls_back_to_default(tmp_path):
    """A schedule whose session was deleted still lands on the Default Agent."""
    db_path = str(tmp_path / "orphan.db")
    conn = sqlite3.connect(db_path)
    conn.executescript(_OLD_SCHEMA)
    # Schedule references a session id that doesn't exist (FK enforcement is
    # off by default in this raw connection, so the orphan persists).
    conn.execute(
        "INSERT INTO schedules (id, session_id, name, prompt, interval_seconds, created_at) "
        "VALUES ('sch1', 'gone', 'daily', 'do it', 3600, '2025-01-01T00:00:00+00:00')"
    )
    conn.commit()
    conn.close()

    db = Database(db_path)
    await db.initialize()
    try:
        default = await db.get_system_agent()
        schedules = await db.load_schedules()
        assert len(schedules) == 1
        assert schedules[0]["agent_id"] == default["id"]
    finally:
        await db.close()
