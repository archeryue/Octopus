"""Tests for bridge_mappings database operations (agent-bound shape)."""

import pytest

from server.database import Database


@pytest.fixture
async def db():
    database = Database(":memory:")
    await database.initialize()
    yield database
    await database.close()


async def _agent_id(db: Database) -> str:
    agent = await db.get_system_agent()
    return agent["id"]


async def _create_session(db: Database, session_id: str, agent_id: str) -> None:
    await db.save_session(
        session_id, "Test", ".", "2024-01-01T00:00:00Z", agent_id=agent_id
    )


class TestBridgeMappings:
    async def test_table_created(self, db: Database):
        cursor = await db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='bridge_mappings'"
        )
        row = await cursor.fetchone()
        assert row is not None

    async def test_save_and_load(self, db: Database):
        agent_id = await _agent_id(db)
        await _create_session(db, "sess1", agent_id)
        await db.save_bridge_mapping("telegram", "12345", agent_id, "sess1")

        rows = await db.load_bridge_mappings()
        assert len(rows) == 1
        assert rows[0] == {
            "platform": "telegram",
            "chat_id": "12345",
            "agent_id": agent_id,
            "session_id": "sess1",
            "verbose": False,
        }

    async def test_bind_without_sticky_session(self, db: Database):
        agent_id = await _agent_id(db)
        await db.save_bridge_mapping("telegram", "12345", agent_id)
        rows = await db.load_bridge_mappings()
        assert rows[0]["agent_id"] == agent_id
        assert rows[0]["session_id"] is None

    async def test_upsert_mapping(self, db: Database):
        agent_id = await _agent_id(db)
        await _create_session(db, "sess1", agent_id)
        await _create_session(db, "sess2", agent_id)

        await db.save_bridge_mapping("telegram", "12345", agent_id, "sess1")
        await db.save_bridge_mapping("telegram", "12345", agent_id, "sess2")

        rows = await db.load_bridge_mappings()
        assert len(rows) == 1
        assert rows[0]["session_id"] == "sess2"

    async def test_set_sticky_session(self, db: Database):
        agent_id = await _agent_id(db)
        await _create_session(db, "sess1", agent_id)
        await db.save_bridge_mapping("telegram", "12345", agent_id)
        await db.set_bridge_sticky_session("telegram", "12345", "sess1")
        rows = await db.load_bridge_mappings()
        assert rows[0]["session_id"] == "sess1"

    async def test_delete_mapping(self, db: Database):
        agent_id = await _agent_id(db)
        await _create_session(db, "sess1", agent_id)
        await db.save_bridge_mapping("telegram", "12345", agent_id, "sess1")
        await db.delete_bridge_mapping("telegram", "12345")

        rows = await db.load_bridge_mappings()
        assert len(rows) == 0

    async def test_delete_nonexistent_is_noop(self, db: Database):
        await db.delete_bridge_mapping("telegram", "99999")
        rows = await db.load_bridge_mappings()
        assert len(rows) == 0

    async def test_session_delete_nulls_sticky_keeps_mapping(self, db: Database):
        """Deleting the sticky session nulls the pointer (ON DELETE SET NULL)
        but keeps the chat's agent binding — the next message opens a fresh
        thread under the same agent."""
        agent_id = await _agent_id(db)
        await _create_session(db, "sess1", agent_id)
        await db.save_bridge_mapping("telegram", "12345", agent_id, "sess1")

        await db.delete_session("sess1")

        rows = await db.load_bridge_mappings()
        assert len(rows) == 1
        assert rows[0]["agent_id"] == agent_id
        assert rows[0]["session_id"] is None

    async def test_clear_bridge_sticky_for_session(self, db: Database):
        agent_id = await _agent_id(db)
        await _create_session(db, "sess1", agent_id)
        await db.save_bridge_mapping("telegram", "111", agent_id, "sess1")
        await db.save_bridge_mapping("discord", "222", agent_id, "sess1")

        nulled = await db.clear_bridge_sticky_for_session("sess1")
        assert nulled == 2
        rows = await db.load_bridge_mappings()
        assert all(r["session_id"] is None for r in rows)
        assert all(r["agent_id"] == agent_id for r in rows)

    async def test_cascade_delete_on_agent_removal(self, db: Database):
        """Deleting an agent cascades to its bridge bindings."""
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).isoformat()
        await db.save_agent(agent_id="ag1", name="Temp", created_at=now, updated_at=now)
        await db.save_bridge_mapping("telegram", "12345", "ag1")
        await db.save_bridge_mapping("discord", "67890", "ag1")

        await db.delete_agent("ag1")

        rows = await db.load_bridge_mappings()
        assert len(rows) == 0

    async def test_multiple_platforms(self, db: Database):
        agent_id = await _agent_id(db)
        await _create_session(db, "sess1", agent_id)
        await _create_session(db, "sess2", agent_id)

        await db.save_bridge_mapping("telegram", "111", agent_id, "sess1")
        await db.save_bridge_mapping("discord", "222", agent_id, "sess2")
        await db.save_bridge_mapping("telegram", "333", agent_id, "sess2")

        rows = await db.load_bridge_mappings()
        assert len(rows) == 3

    async def test_load_empty(self, db: Database):
        rows = await db.load_bridge_mappings()
        assert rows == []


class TestBridgeVerbose:
    async def test_defaults_to_quiet(self, db: Database):
        agent_id = await _agent_id(db)
        await db.save_bridge_mapping("telegram", "12345", agent_id)
        rows = await db.load_bridge_mappings()
        assert rows[0]["verbose"] is False

    async def test_set_and_load(self, db: Database):
        agent_id = await _agent_id(db)
        await db.save_bridge_mapping("telegram", "12345", agent_id)
        await db.set_bridge_verbose("telegram", "12345", True)
        rows = await db.load_bridge_mappings()
        assert rows[0]["verbose"] is True
        await db.set_bridge_verbose("telegram", "12345", False)
        rows = await db.load_bridge_mappings()
        assert rows[0]["verbose"] is False

    async def test_rebind_preserves_verbose(self, db: Database):
        """Re-saving a mapping (e.g. /agent rebind) must not reset verbose."""
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).isoformat()
        agent_id = await _agent_id(db)
        await db.save_agent(agent_id="ag2", name="Helper", created_at=now, updated_at=now)
        await db.save_bridge_mapping("telegram", "12345", agent_id)
        await db.set_bridge_verbose("telegram", "12345", True)

        # Rebind to a different agent, clearing the sticky session.
        await db.save_bridge_mapping("telegram", "12345", "ag2", None)

        rows = await db.load_bridge_mappings()
        assert rows[0]["agent_id"] == "ag2"
        assert rows[0]["session_id"] is None
        assert rows[0]["verbose"] is True
