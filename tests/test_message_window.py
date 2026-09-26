"""A transcript arrives windowed, and pages back by cursor (§4 B2).

46,448 messages live in the production database across 66 sessions, and the
largest single session holds 4,873 — which `GET /api/sessions/{id}` used to
return in full every time the session was opened. It now returns the most
recent `MESSAGE_WINDOW`, plus the two fields a client needs to ask for the rest,
and `GET …/messages?before_seq=` serves the rest.

The boundary is what these tests are for: the window must end where the cursor
starts, with nothing skipped and nothing served twice.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from server.database import Database
from server.main import app
from server.routers.sessions import MESSAGE_WINDOW
from server.session_manager import session_manager

TOKEN = "changeme"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
async def client():
    db = Database(":memory:")
    await db.initialize()
    session_manager.sessions.clear()
    await session_manager.initialize(db)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c

    await db.close()


async def _imported(client, count: int) -> str:
    """A session with `count` user messages, each naming its own index."""
    resp = await client.post(
        "/api/sessions/import",
        json={
            "name": "Long",
            "working_dir": "/tmp",
            "messages": [
                {"role": "user", "type": "text", "content": f"msg-{i}"}
                for i in range(count)
            ],
        },
        headers=HEADERS,
    )
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["id"]


@pytest.mark.asyncio
async def test_a_short_transcript_arrives_whole(client):
    sid = await _imported(client, 5)
    data = (await client.get(f"/api/sessions/{sid}", headers=HEADERS)).json()
    assert [m["content"] for m in data["messages"]] == [f"msg-{i}" for i in range(5)]
    assert data["oldest_loaded_seq"] == 0
    # Nothing before seq 0, so the client must not ask.
    assert data["has_more_messages"] is False
    assert data["next_message_seq"] == 5


@pytest.mark.asyncio
async def test_an_empty_transcript_has_no_cursor(client):
    resp = await client.post(
        "/api/sessions", json={"name": "Empty"}, headers=HEADERS
    )
    sid = resp.json()["id"]
    data = (await client.get(f"/api/sessions/{sid}", headers=HEADERS)).json()
    assert data["messages"] == []
    assert data["oldest_loaded_seq"] is None
    assert data["has_more_messages"] is False


@pytest.mark.asyncio
async def test_a_long_transcript_arrives_windowed_to_the_newest(client):
    total = MESSAGE_WINDOW + 40
    sid = await _imported(client, total)
    data = (await client.get(f"/api/sessions/{sid}", headers=HEADERS)).json()

    assert len(data["messages"]) == MESSAGE_WINDOW
    # The *newest* window, not the first page: the conversation opens where the
    # user left it.
    assert data["messages"][-1]["content"] == f"msg-{total - 1}"
    assert data["messages"][0]["content"] == f"msg-{total - MESSAGE_WINDOW}"
    assert data["oldest_loaded_seq"] == total - MESSAGE_WINDOW
    assert data["has_more_messages"] is True
    # The dedup baseline still counts the whole transcript, not the window.
    assert data["next_message_seq"] == total
    assert data["message_count"] == total


@pytest.mark.asyncio
async def test_the_cursor_returns_the_page_before_it_with_no_gap(client):
    total = MESSAGE_WINDOW + 40
    sid = await _imported(client, total)
    first = (await client.get(f"/api/sessions/{sid}", headers=HEADERS)).json()

    page = (
        await client.get(
            f"/api/sessions/{sid}/messages",
            params={"before_seq": first["oldest_loaded_seq"]},
            headers=HEADERS,
        )
    ).json()
    assert len(page["messages"]) == 40
    assert page["messages"][0]["content"] == "msg-0"
    # Ends exactly where the window began — no gap, no overlap.
    assert page["messages"][-1]["seq"] == first["oldest_loaded_seq"] - 1
    assert page["oldest_loaded_seq"] == 0
    assert page["has_more_messages"] is False

    # The two halves reconstruct the transcript exactly once.
    joined = [m["content"] for m in page["messages"] + first["messages"]]
    assert joined == [f"msg-{i}" for i in range(total)]


@pytest.mark.asyncio
async def test_the_page_size_is_capped_and_honours_limit(client):
    sid = await _imported(client, 50)
    page = (
        await client.get(
            f"/api/sessions/{sid}/messages",
            params={"before_seq": 50, "limit": 10},
            headers=HEADERS,
        )
    ).json()
    assert len(page["messages"]) == 10
    # The newest ten below the cursor, so paging backwards is contiguous.
    assert page["messages"][-1]["seq"] == 49
    assert page["oldest_loaded_seq"] == 40
    assert page["has_more_messages"] is True

    over = await client.get(
        f"/api/sessions/{sid}/messages",
        params={"before_seq": 50, "limit": 9999},
        headers=HEADERS,
    )
    assert over.status_code == 422


@pytest.mark.asyncio
async def test_the_cursor_at_zero_is_the_end_of_the_transcript(client):
    sid = await _imported(client, 5)
    page = (
        await client.get(
            f"/api/sessions/{sid}/messages",
            params={"before_seq": 0},
            headers=HEADERS,
        )
    ).json()
    assert page["messages"] == []
    assert page["has_more_messages"] is False


@pytest.mark.asyncio
async def test_paging_an_unknown_session_is_404(client):
    resp = await client.get(
        "/api/sessions/nope/messages", params={"before_seq": 10}, headers=HEADERS
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_an_archived_session_is_windowed_and_pages_the_same_way(client):
    total = MESSAGE_WINDOW + 7
    sid = await _imported(client, total)
    assert (
        await client.post(f"/api/sessions/{sid}/archive", headers=HEADERS)
    ).status_code == 201

    data = (await client.get(f"/api/sessions/{sid}", headers=HEADERS)).json()
    assert data["archived"] is True
    assert len(data["messages"]) == MESSAGE_WINDOW
    assert data["has_more_messages"] is True
    # `len(messages)` stopped being the count when the window landed; both of
    # these used to be derived from it.
    assert data["message_count"] == total
    assert data["next_message_seq"] == total

    page = (
        await client.get(
            f"/api/sessions/{sid}/messages",
            params={"before_seq": data["oldest_loaded_seq"]},
            headers=HEADERS,
        )
    ).json()
    assert len(page["messages"]) == 7
    assert page["messages"][0]["content"] == "msg-0"


@pytest.mark.asyncio
async def test_paging_requires_auth(client):
    sid = await _imported(client, 3)
    resp = await client.get(f"/api/sessions/{sid}/messages?before_seq=3")
    assert resp.status_code in (401, 403)
