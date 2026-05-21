"""Gmail connector MCP server (connectors.md §6.2): search projection, body
decode, draft/send, label, and the 401→reconnect path. HTTP mocked by
monkeypatching the module's httpx; tools are called directly."""

from __future__ import annotations

import base64

import pytest

from server.mcp_servers.connectors import gmail as gm


class FakeResp:
    def __init__(self, status=200, json_body=None, headers=None):
        self.status_code = status
        self._json = json_body
        self.text = ""
        self.headers = headers or {}
        self.content = b"x" if json_body is not None else b""

    def json(self):
        return self._json


@pytest.fixture
def authed(monkeypatch):
    monkeypatch.setattr(gm.ctx, "access_token", lambda: "tok")
    recon: list[str] = []
    monkeypatch.setattr(
        gm.ctx, "mark_needs_reconnect", lambda code="invalid_grant": recon.append(code)
    )
    return recon


def _patch(monkeypatch, handler):
    monkeypatch.setattr(gm.httpx, "request", handler)


def _b64url(s: bytes) -> str:
    return base64.urlsafe_b64encode(s).decode().rstrip("=")


def test_search_lists_and_projects(monkeypatch, authed):
    def handler(method, url, **kw):
        if url.endswith("/users/me/messages"):
            return FakeResp(json_body={"messages": [{"id": "m1"}]})
        if "/users/me/messages/m1" in url:
            return FakeResp(
                json_body={
                    "id": "m1",
                    "threadId": "t1",
                    "snippet": "hi there",
                    "payload": {
                        "headers": [
                            {"name": "From", "value": "alice@x.com"},
                            {"name": "Subject", "value": "Hello"},
                        ]
                    },
                }
            )
        return FakeResp(status=404)

    _patch(monkeypatch, handler)
    out = gm.search(query="is:unread", limit=5)
    assert "alice@x.com" in out
    assert "Hello" in out
    assert "hi there" in out


def test_get_decodes_plain_body(monkeypatch, authed):
    payload = {
        "headers": [{"name": "Subject", "value": "S"}],
        "mimeType": "multipart/alternative",
        "parts": [
            {"mimeType": "text/html", "body": {"data": _b64url(b"<p>nope</p>")}},
            {"mimeType": "text/plain", "body": {"data": _b64url(b"Body text here")}},
        ],
    }
    _patch(
        monkeypatch,
        lambda m, u, **k: FakeResp(json_body={"id": "m1", "threadId": "t1", "payload": payload}),
    )
    out = gm.get(message_id="m1")
    assert "Body text here" in out


def test_list_labels(monkeypatch, authed):
    _patch(
        monkeypatch,
        lambda m, u, **k: FakeResp(
            json_body={"labels": [{"id": "INBOX", "name": "INBOX"}]}
        ),
    )
    out = gm.list_labels()
    assert "INBOX" in out


def test_create_draft_builds_raw(monkeypatch, authed):
    cap = {}

    def handler(method, url, **kw):
        cap["method"], cap["url"], cap["json"] = method, url, kw.get("json")
        return FakeResp(json_body={"id": "d1"})

    _patch(monkeypatch, handler)
    out = gm.create_draft(to="bob@x.com", subject="Hi", body="Hello Bob")
    assert cap["method"] == "POST" and cap["url"].endswith("/users/me/drafts")
    raw = cap["json"]["message"]["raw"]
    decoded = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode()
    assert "bob@x.com" in decoded and "Hello Bob" in decoded
    assert '"draft_id": "d1"' in out


def test_send_draft(monkeypatch, authed):
    cap = {}

    def handler(method, url, **kw):
        cap["url"], cap["json"] = url, kw.get("json")
        return FakeResp(json_body={"id": "sent-1"})

    _patch(monkeypatch, handler)
    out = gm.send_draft(draft_id="d1")
    assert cap["url"].endswith("/users/me/drafts/send")
    assert cap["json"] == {"id": "d1"}
    assert '"sent": true' in out


def test_label_modifies(monkeypatch, authed):
    cap = {}

    def handler(method, url, **kw):
        cap["json"] = kw.get("json")
        return FakeResp(json_body={"id": "m1", "labelIds": ["INBOX", "STARRED"]})

    _patch(monkeypatch, handler)
    out = gm.label(message_id="m1", label_ids=["STARRED"])
    assert cap["json"] == {"addLabelIds": ["STARRED"]}
    assert "STARRED" in out


def test_401_marks_reconnect(monkeypatch, authed):
    _patch(monkeypatch, lambda m, u, **k: FakeResp(status=401))
    out = gm.get(message_id="m1")
    assert "reconnect" in out.lower()
    assert authed == ["invalid_grant"]


def test_token_unavailable(monkeypatch):
    monkeypatch.setattr(gm.ctx, "access_token", lambda: None)
    assert "reconnect" in gm.list_labels().lower() or "unavailable" in gm.list_labels().lower()
