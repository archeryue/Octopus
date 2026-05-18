"""Tests for the notifier framework — DB CRUD, manager, webhook."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from server.database import Database
from server.notifiers import NotifierEvent, NotifierManager
from server.notifiers.webhook import WebhookNotifier


# ---------------------------------------------------------------------------
# database CRUD
# ---------------------------------------------------------------------------


@pytest.fixture
async def db():
    d = Database(":memory:")
    await d.initialize()
    yield d
    await d.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@pytest.mark.asyncio
async def test_save_and_load_notifier(db):
    await db.save_notifier(
        notifier_id="n-1",
        type="webhook",
        label="My hook",
        config={"url": "https://example.com/hook"},
        created_at=_now(),
    )
    rows = await db.load_notifiers()
    assert len(rows) == 1
    assert rows[0]["id"] == "n-1"
    assert rows[0]["type"] == "webhook"
    assert rows[0]["label"] == "My hook"
    assert rows[0]["config"] == {"url": "https://example.com/hook"}
    assert rows[0]["enabled"] is True


@pytest.mark.asyncio
async def test_update_notifier(db):
    await db.save_notifier("n-2", "webhook", "Old", {"url": "x"}, _now())
    await db.update_notifier(
        "n-2", label="New", config={"url": "y"}, enabled=False
    )
    rows = await db.load_notifiers()
    assert rows[0]["label"] == "New"
    assert rows[0]["config"] == {"url": "y"}
    assert rows[0]["enabled"] is False


@pytest.mark.asyncio
async def test_delete_notifier(db):
    await db.save_notifier("n-3", "webhook", "L", {"url": "x"}, _now())
    assert await db.delete_notifier("n-3") is True
    assert await db.delete_notifier("n-3") is False
    rows = await db.load_notifiers()
    assert rows == []


# ---------------------------------------------------------------------------
# manager dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_manager_loads_only_enabled(db):
    await db.save_notifier("on", "webhook", "On", {"url": "a"}, _now())
    await db.save_notifier("off", "webhook", "Off", {"url": "b"}, _now(), enabled=False)
    mgr = NotifierManager()
    mgr.set_db(db)
    await mgr.load()
    ids = [n.id for n in mgr.list()]
    assert ids == ["on"]


@pytest.mark.asyncio
async def test_manager_fires_to_all_registered(db, monkeypatch):
    await db.save_notifier("a", "webhook", "A", {"url": "https://a"}, _now())
    await db.save_notifier("b", "webhook", "B", {"url": "https://b"}, _now())
    mgr = NotifierManager()
    mgr.set_db(db)
    await mgr.load()

    sent: list[tuple[str, NotifierEvent]] = []

    async def fake_send(self, event):
        sent.append((self.id, event))

    monkeypatch.setattr(WebhookNotifier, "send", fake_send)

    event = NotifierEvent(
        type="session_idle", title="t", message="m", session_id="s1"
    )
    await mgr.fire(event)

    sent_ids = sorted(s[0] for s in sent)
    assert sent_ids == ["a", "b"]
    assert all(s[1].type == "session_idle" for s in sent)


@pytest.mark.asyncio
async def test_manager_swallows_per_notifier_exceptions(db, monkeypatch):
    """One bad target must not poison the rest."""
    await db.save_notifier("good", "webhook", "G", {"url": "https://g"}, _now())
    await db.save_notifier("bad", "webhook", "B", {"url": "https://b"}, _now())
    mgr = NotifierManager()
    mgr.set_db(db)
    await mgr.load()

    sent: list[str] = []

    async def maybe_raise(self, event):
        if self.id == "bad":
            raise RuntimeError("boom")
        sent.append(self.id)

    monkeypatch.setattr(WebhookNotifier, "send", maybe_raise)
    await mgr.fire(NotifierEvent(type="x", title="t", message="m"))
    assert sent == ["good"]


# ---------------------------------------------------------------------------
# webhook target
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_webhook_posts_json(monkeypatch):
    """WebhookNotifier issues an httpx POST with the event payload as JSON."""
    posted: dict = {}

    class FakeResponse:
        status_code = 200

    class FakeClient:
        def __init__(self, *_, **__):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def post(self, url, json):
            posted["url"] = url
            posted["json"] = json
            return FakeResponse()

    monkeypatch.setattr("server.notifiers.webhook.httpx.AsyncClient", FakeClient)

    n = WebhookNotifier(
        id="w-1", label="L", config={"url": "https://example.com/wh"}
    )
    await n.send(
        NotifierEvent(
            type="session_idle",
            title="Hi",
            message="Done.",
            session_id="s-1",
            session_name="My session",
        )
    )
    assert posted["url"] == "https://example.com/wh"
    assert posted["json"]["type"] == "session_idle"
    assert posted["json"]["session_id"] == "s-1"


@pytest.mark.asyncio
async def test_webhook_skips_when_no_url(caplog):
    n = WebhookNotifier(id="w", label="L", config={})
    await n.send(NotifierEvent(type="x", title="t", message="m"))
    # No exception, just a logged warning.
    assert any("no URL" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_session_manager_fires_idle_notification(monkeypatch):
    """When a session's queue drains, the wired notifier_manager is called."""
    from server.session_manager import SessionManager

    sm = SessionManager()
    fake_mgr = AsyncMock()
    sm.set_notifier_manager(fake_mgr)

    class _Sess:
        id = "s-1"
        name = "My session"

    await sm._fire_session_idle_notification(_Sess())  # type: ignore[arg-type]

    fake_mgr.fire.assert_awaited_once()
    fired_event = fake_mgr.fire.call_args.args[0]
    assert fired_event.type == "session_idle"
    assert fired_event.session_id == "s-1"
    assert fired_event.session_name == "My session"
