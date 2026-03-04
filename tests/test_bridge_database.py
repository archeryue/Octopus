"""Tests for bridge_mappings database operations."""

import pytest

from server.database import Database


@pytest.fixture
async def db():
    database = Database(":memory:")
    await database.initialize()
    yield database
    await database.close()


async def _create_session(db: Database, session_id: str = "abc123") -> None:
    await db.save_session(session_id, "Test", ".", "2024-01-01T00:00:00Z")


class TestBridgeMappings:
    async def test_table_created(self, db: Database):
        cursor = await db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='bridge_mappings'"
        )
        row = await cursor.fetchone()
        assert row is not None

    async def test_save_and_load(self, db: Database):
        await _create_session(db, "sess1")
        await db.save_bridge_mapping("telegram", "12345", "sess1")

        rows = await db.load_bridge_mappings()
        assert len(rows) == 1
        assert rows[0] == {
            "platform": "telegram",
            "chat_id": "12345",
            "session_id": "sess1",
        }

    async def test_upsert_mapping(self, db: Database):
        await _create_session(db, "sess1")
        await _create_session(db, "sess2")

        await db.save_bridge_mapping("telegram", "12345", "sess1")
        await db.save_bridge_mapping("telegram", "12345", "sess2")

        rows = await db.load_bridge_mappings()
        assert len(rows) == 1
        assert rows[0]["session_id"] == "sess2"

    async def test_delete_mapping(self, db: Database):
        await _create_session(db, "sess1")
        await db.save_bridge_mapping("telegram", "12345", "sess1")
        await db.delete_bridge_mapping("telegram", "12345")

        rows = await db.load_bridge_mappings()
        assert len(rows) == 0

    async def test_delete_nonexistent_is_noop(self, db: Database):
        await db.delete_bridge_mapping("telegram", "99999")
        rows = await db.load_bridge_mappings()
        assert len(rows) == 0

    async def test_cascade_delete_on_session_removal(self, db: Database):
        await _create_session(db, "sess1")
        await db.save_bridge_mapping("telegram", "12345", "sess1")
        await db.save_bridge_mapping("discord", "67890", "sess1")

        await db.delete_session("sess1")

        rows = await db.load_bridge_mappings()
        assert len(rows) == 0

    async def test_multiple_platforms(self, db: Database):
        await _create_session(db, "sess1")
        await _create_session(db, "sess2")

        await db.save_bridge_mapping("telegram", "111", "sess1")
        await db.save_bridge_mapping("discord", "222", "sess2")
        await db.save_bridge_mapping("telegram", "333", "sess2")

        rows = await db.load_bridge_mappings()
        assert len(rows) == 3

    async def test_load_empty(self, db: Database):
        rows = await db.load_bridge_mappings()
        assert rows == []
