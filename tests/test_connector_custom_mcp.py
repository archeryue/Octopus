"""Generic custom-connector MCP server (connectors.md custom-connectors): the
authenticated `request` tool. HTTP mocked by monkeypatching the module httpx;
the tool is a plain callable in this FastMCP version."""

from __future__ import annotations

import pytest

from server.mcp_servers.connectors import custom as cu


class FakeResp:
    def __init__(self, status=200, json_body=None, text="", headers=None):
        self.status_code = status
        self._json = json_body
        self.text = text
        self.headers = headers or {}
        self.content = b"x" if (json_body is not None or text) else b""

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


@pytest.fixture
def authed(monkeypatch):
    monkeypatch.setattr(cu.ctx, "access_token", lambda: "tok")
    recon: list[str] = []
    monkeypatch.setattr(
        cu.ctx, "mark_needs_reconnect", lambda code="invalid_grant": recon.append(code)
    )
    monkeypatch.setenv("OCTOPUS_CONNECTOR_API_BASE", "https://api.x.com")
    return recon


def test_request_get_projects_url_and_auth(monkeypatch, authed):
    cap: dict = {}

    def handler(method, url, headers=None, params=None, json=None, timeout=None):
        cap.update(method=method, url=url, headers=headers, params=params, json=json)
        return FakeResp(json_body={"ok": True})

    monkeypatch.setattr(cu.httpx, "request", handler)
    out = cu.request(method="get", path="/issues", query={"q": "open"})
    assert cap["method"] == "GET"
    assert cap["url"] == "https://api.x.com/issues"  # api base + path, no dup slash
    assert cap["params"] == {"q": "open"}
    assert cap["headers"]["Authorization"] == "Bearer tok"
    assert '"ok": true' in out


def test_request_post_sends_body(monkeypatch, authed):
    cap: dict = {}

    def handler(method, url, **k):
        cap["method"], cap["json"] = method, k.get("json")
        return FakeResp(json_body={"id": 1})

    monkeypatch.setattr(cu.httpx, "request", handler)
    cu.request(method="post", path="issues", body={"title": "hi"})
    assert cap["method"] == "POST" and cap["json"] == {"title": "hi"}


def test_request_401_marks_reconnect(monkeypatch, authed):
    monkeypatch.setattr(cu.httpx, "request", lambda *a, **k: FakeResp(status=401))
    out = cu.request(method="get", path="/x")
    assert "reconnect" in out.lower()
    assert authed == ["invalid_grant"]


def test_request_no_api_base(monkeypatch):
    monkeypatch.setattr(cu.ctx, "access_token", lambda: "tok")
    monkeypatch.delenv("OCTOPUS_CONNECTOR_API_BASE", raising=False)
    assert "misconfigured" in cu.request(method="get", path="/x").lower()


def test_request_no_token(monkeypatch):
    monkeypatch.setattr(cu.ctx, "access_token", lambda: None)
    out = cu.request(method="get", path="/x").lower()
    assert "unavailable" in out or "reconnect" in out
