"""Unit tests for the `schedule` MCP stdio server tool surface
(schedule-tool.md §5).

Same shape as `tests/test_ask_agent_mcp.py`: the FastMCP-wrapped tool
functions are invoked directly with httpx mocked, so the request shape, the
scoping (session id from the env, never a parameter) and the text the model
reads back are all verified without a live FastAPI. The routes behind them
are covered by `tests/test_schedule_tool.py`.
"""

from __future__ import annotations

import pytest


def _call(tool: str, **kwargs):
    """Invoke a FastMCP-wrapped tool function directly — FastMCP versions
    differ in whether they expose the raw callable or a `.fn`."""
    from server.mcp_servers import schedule as srv

    fn = getattr(srv, tool)
    try:
        return fn(**kwargs)
    except TypeError:
        return fn.fn(**kwargs)  # type: ignore[attr-defined]


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("OCTOPUS_API_BASE", "http://x")
    monkeypatch.setenv("OCTOPUS_SESSION_ID", "s1")
    monkeypatch.setenv("OCTOPUS_AUTH_TOKEN", "t")


class FakeResp:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


ROW = {
    "id": "sch123",
    "name": "Morning build check",
    "prompt": "check the build",
    "cron": "0 9 * * 1-5",
    "interval_seconds": None,
    "timezone": "America/Los_Angeles",
    "recurrence_label": "Weekdays at 09:00",
    "enabled": True,
    "origin_session_id": "s1",
    "next_run_at": "2026-09-22T09:00:00-07:00",
}


# --- misconfiguration ------------------------------------------------------ #


@pytest.mark.parametrize(
    "tool,kwargs",
    [
        ("create_schedule", {"prompt": "x", "interval_seconds": 600}),
        ("list_schedules", {}),
        ("update_schedule", {"schedule_id": "a", "enabled": False}),
        ("delete_schedule", {"schedule_id": "a"}),
    ],
)
def test_misconfigured_env(monkeypatch, tool, kwargs):
    for var in ("OCTOPUS_API_BASE", "OCTOPUS_SESSION_ID", "OCTOPUS_AUTH_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    assert "misconfigured" in _call(tool, **kwargs).lower()


# --- create ---------------------------------------------------------------- #


def test_create_posts_to_the_session_scoped_route(env, monkeypatch):
    posted: dict = {}

    def fake_post(url, json=None, headers=None, timeout=None):  # noqa: ARG001
        posted.update(url=url, body=json, headers=headers)
        return FakeResp(201, ROW)

    import httpx

    monkeypatch.setattr(httpx, "post", fake_post)

    out = _call(
        "create_schedule",
        prompt="check the build",
        name="Morning build check",
        cron="0 9 * * 1-5",
        timezone="America/Los_Angeles",
    )
    # The session comes from the env — the model never gets to name it.
    assert posted["url"] == "http://x/api/sessions/s1/schedules"
    assert posted["body"] == {
        "prompt": "check the build",
        "in_session": True,
        "name": "Morning build check",
        "cron": "0 9 * * 1-5",
        "timezone": "America/Los_Angeles",
    }
    assert posted["headers"]["Authorization"] == "Bearer t"
    # What comes back has to be quotable to the user: id, when, and how to
    # change it.
    assert "sch123" in out
    assert "Weekdays at 09:00" in out
    assert "2026-09-22T09:00:00-07:00" in out
    assert "fires into this conversation" in out


def test_create_omits_unset_recurrence_fields(env, monkeypatch):
    """Unset arguments are left out of the body entirely, so the server's
    "exactly one recurrence" check sees exactly what the model chose."""
    posted: dict = {}

    def fake_post(url, json=None, headers=None, timeout=None):  # noqa: ARG001
        posted.update(body=json)
        return FakeResp(201, {**ROW, "cron": None, "interval_seconds": 900})

    import httpx

    monkeypatch.setattr(httpx, "post", fake_post)
    _call("create_schedule", prompt="poll", interval_seconds=900, in_session=False)
    assert posted["body"] == {
        "prompt": "poll",
        "in_session": False,
        "interval_seconds": 900,
    }


def test_create_requires_a_prompt(env):
    assert "prompt" in _call("create_schedule", prompt="   ", cron="0 9 * * *").lower()


def test_create_relays_the_servers_validation_message(env, monkeypatch):
    """A 422 is our own sentence explaining what to pass instead — it reaches
    the model verbatim rather than as a status code."""
    import httpx

    monkeypatch.setattr(
        httpx,
        "post",
        lambda *a, **k: FakeResp(
            422, {"detail": "Pass exactly one of `cron`, `interval_seconds` or `run_at`."}
        ),
    )
    out = _call("create_schedule", prompt="x", cron="0 9 * * *", interval_seconds=60)
    assert "exactly one" in out


def test_create_reports_an_unreachable_host(env, monkeypatch):
    import httpx

    def boom(*a, **k):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx, "post", boom)
    assert "failed to reach Octopus" in _call(
        "create_schedule", prompt="x", interval_seconds=600
    )


# --- list ------------------------------------------------------------------ #


def test_list_renders_one_line_each(env, monkeypatch):
    import httpx

    rows = [
        ROW,
        {
            **ROW,
            "id": "sch456",
            "name": "Queue poll",
            "cron": None,
            "interval_seconds": 900,
            "timezone": None,
            "recurrence_label": "Every 15m",
            "enabled": False,
            "origin_session_id": None,
            "next_run_at": None,
        },
    ]
    monkeypatch.setattr(httpx, "get", lambda *a, **k: FakeResp(200, rows))
    out = _call("list_schedules")
    assert "2 schedules" in out
    assert "[sch123]" in out and "[sch456]" in out
    assert "active" in out and "paused" in out
    assert "fires in its own session" in out
    # The cron is printed next to its reading — the label alone is ambiguous
    # when you're about to edit the expression.
    assert "(0 9 * * 1-5, America/Los_Angeles)" in out


def test_list_says_so_when_empty(env, monkeypatch):
    import httpx

    monkeypatch.setattr(httpx, "get", lambda *a, **k: FakeResp(200, []))
    assert "No schedules" in _call("list_schedules")


# --- update ---------------------------------------------------------------- #


def test_update_patches_only_what_changed(env, monkeypatch):
    sent: dict = {}

    def fake_patch(url, json=None, headers=None, timeout=None):  # noqa: ARG001
        sent.update(url=url, body=json)
        return FakeResp(200, {**ROW, "enabled": False, "next_run_at": None})

    import httpx

    monkeypatch.setattr(httpx, "patch", fake_patch)
    out = _call("update_schedule", schedule_id="sch123", enabled=False)
    assert sent["url"] == "http://x/api/sessions/s1/schedules/sch123"
    assert sent["body"] == {"enabled": False}
    assert "paused" in out


def test_update_needs_something_to_change(env):
    out = _call("update_schedule", schedule_id="sch123")
    assert "nothing to change" in out.lower()


def test_update_needs_an_id(env):
    assert "schedule_id" in _call("update_schedule", schedule_id="  ")


def test_update_reports_an_unknown_schedule(env, monkeypatch):
    """Another agent's schedule is a 404 here; the message says so rather
    than looking like a transport failure."""
    import httpx

    monkeypatch.setattr(
        httpx, "patch", lambda *a, **k: FakeResp(404, {"detail": "Schedule not found"})
    )
    out = _call("update_schedule", schedule_id="theirs", enabled=False)
    assert "Schedule not found" in out and "404" in out


# --- delete ---------------------------------------------------------------- #


def test_delete_calls_the_scoped_route(env, monkeypatch):
    sent: dict = {}

    def fake_delete(url, headers=None, timeout=None):  # noqa: ARG001
        sent.update(url=url)
        return FakeResp(204)

    import httpx

    monkeypatch.setattr(httpx, "delete", fake_delete)
    out = _call("delete_schedule", schedule_id=" sch123 ")
    assert sent["url"] == "http://x/api/sessions/s1/schedules/sch123"
    assert "Deleted schedule sch123" in out


def test_delete_needs_an_id(env):
    assert "schedule_id" in _call("delete_schedule", schedule_id="")


# --- the tool surface itself ----------------------------------------------- #


@pytest.mark.asyncio
async def test_exposed_tool_names_are_the_short_forms():
    """`mcp__schedule__create` etc. — the prefix is the mount key, so the
    tools themselves must be named create/list/update/delete."""
    from server.mcp_servers.schedule import mcp

    tools = await mcp.list_tools()
    assert {t.name for t in tools} == {"create", "list", "update", "delete"}
    # Every one carries a docstring: it is the only place the model learns
    # what a crontab field means here.
    assert all((t.description or "").strip() for t in tools)
