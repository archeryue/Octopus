"""The sidebar-pins migration (sidebar-pins.md §4) on a database that predates it.

Every agent and application that existed before pins must come out pinned, in
the order the sidebar already showed them — the Default Agent first, then by
creation — so the first boot on the new schema changes nothing the user sees.
Run twice to prove the backfill is idempotent.
"""

import sqlite3

import pytest

from server.database import Database

# `agents` and `applications` exactly as they were before `pinned`/`pin_order`.
_PRE_PIN_SCHEMA = """
CREATE TABLE agents (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    avatar TEXT,
    system_prompt TEXT NOT NULL DEFAULT '',
    model TEXT,
    credential_id TEXT,
    backend TEXT NOT NULL DEFAULT 'claude-code',
    mcp_servers TEXT NOT NULL DEFAULT '["ask","bg","ask_agent","research","schedule"]',
    tool_allow TEXT NOT NULL DEFAULT '',
    tool_deny  TEXT NOT NULL DEFAULT '',
    subagents TEXT NOT NULL DEFAULT '[]',
    is_system INTEGER NOT NULL DEFAULT 0,
    archived INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE applications (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    icon TEXT,
    icon_src TEXT,
    agent_id TEXT,
    session_id TEXT,
    app_dir TEXT NOT NULL,
    entrypoint TEXT NOT NULL DEFAULT 'index.html',
    status TEXT NOT NULL DEFAULT 'building',
    error TEXT,
    archived INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_built_at TEXT
);
"""


def _seed(path: str) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(_PRE_PIN_SCHEMA)
    # Inserted out of creation order, with the Default Agent created last, so
    # the backfill has to sort rather than trust rowid.
    for agent_id, name, created, is_system, archived in (
        ("b", "Beta", "2026-02-01", 0, 0),
        ("a", "Alpha", "2026-01-01", 0, 0),
        ("z", "Zed", "2026-03-01", 0, 1),
        ("octo", "Octo", "2026-04-01", 1, 0),
    ):
        conn.execute(
            "INSERT INTO agents (id, name, is_system, archived, created_at, "
            "updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (agent_id, name, is_system, archived, created, created),
        )
    for app_id, created in (("app2", "2026-02-01"), ("app1", "2026-01-01")):
        conn.execute(
            "INSERT INTO applications (id, name, app_dir, created_at, "
            "updated_at) VALUES (?, ?, '/tmp/x', ?, ?)",
            (app_id, app_id, created, created),
        )
    conn.commit()
    conn.close()


def _order(rows: list[dict]) -> list[tuple[str, bool, int]]:
    return [
        (r["id"], r["pinned"], r["pin_order"])
        for r in sorted(rows, key=lambda r: r["pin_order"])
    ]


@pytest.mark.asyncio
async def test_existing_rows_come_out_pinned_in_sidebar_order(tmp_path):
    path = str(tmp_path / "pre-pins.db")
    _seed(path)

    for _ in range(2):  # the second boot must change nothing
        db = Database(path)
        await db.initialize()
        agents = await db.load_agents(include_archived=True)
        apps = await db.load_applications(include_archived=True)
        await db.close()

        assert _order(agents) == [
            ("octo", True, 1),
            ("a", True, 2),
            ("b", True, 3),
            ("z", True, 4),
        ]
        assert _order(apps) == [("app1", True, 1), ("app2", True, 2)]


@pytest.mark.asyncio
async def test_rows_written_after_the_migration_append(tmp_path):
    path = str(tmp_path / "pre-pins.db")
    _seed(path)
    db = Database(path)
    await db.initialize()
    await db.save_agent(
        agent_id="new", name="New", created_at="2026-05-01", updated_at="2026-05-01"
    )
    agent = await db.get_agent("new")
    await db.close()
    assert agent is not None
    assert agent["pinned"] is True
    assert agent["pin_order"] == 5
