"""Unit tests for the `ask_agent` MCP stdio server tool surface.

Mirrors the inline MCP tests in `tests/test_bg_tasks.py`: we invoke
the FastMCP-wrapped tool functions directly with httpx mocked, so we
don't need a live FastAPI to verify the tool's request shape. End-to-
end coverage (a real ask_agent call into a real DelegationManager
into a real child session) is exercised by `tests/test_delegations.py`
plus the integration tests added in Phase 5.
"""

from __future__ import annotations


def _call(tool: str, **kwargs):
    """Invoke a FastMCP-wrapped tool function directly. Pattern lifted
    from tests/test_bg_tasks.py — FastMCP versions differ in whether
    they expose the raw callable or a `.fn` attribute. The parameter
    is named `tool` (not `name`) because the `ask_agent` tool itself
    takes a `name=` kwarg and `_call("ask_agent", name=...)` would
    otherwise collide."""
    from server.mcp_servers import ask_agent as srv

    fn = getattr(srv, tool)
    try:
        return fn(**kwargs)
    except TypeError:
        return fn.fn(**kwargs)  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# ask_agent
# ---------------------------------------------------------------------------


def test_ask_agent_misconfigured(monkeypatch):
    monkeypatch.delenv("OCTOPUS_API_BASE", raising=False)
    monkeypatch.delenv("OCTOPUS_SESSION_ID", raising=False)
    monkeypatch.delenv("OCTOPUS_AUTH_TOKEN", raising=False)
    out = _call("ask_agent", name="Vera", request="r")
    assert "misconfigured" in out.lower()


def test_ask_agent_rejects_empty_name(monkeypatch):
    monkeypatch.setenv("OCTOPUS_API_BASE", "http://x")
    monkeypatch.setenv("OCTOPUS_SESSION_ID", "s")
    monkeypatch.setenv("OCTOPUS_AUTH_TOKEN", "t")
    out = _call("ask_agent", name="   ", request="r")
    assert "name" in out.lower() and "non-empty" in out.lower()


def test_ask_agent_rejects_empty_request(monkeypatch):
    monkeypatch.setenv("OCTOPUS_API_BASE", "http://x")
    monkeypatch.setenv("OCTOPUS_SESSION_ID", "s")
    monkeypatch.setenv("OCTOPUS_AUTH_TOKEN", "t")
    out = _call("ask_agent", name="Vera", request="   ")
    assert "request" in out.lower() and "non-empty" in out.lower()


def test_ask_agent_success(monkeypatch):
    monkeypatch.setenv("OCTOPUS_API_BASE", "http://x")
    monkeypatch.setenv("OCTOPUS_SESSION_ID", "s")
    monkeypatch.setenv("OCTOPUS_AUTH_TOKEN", "t")
    posted: dict = {}

    class FakeResp:
        status_code = 201
        text = ""

        def json(self):
            return {
                "delegation_id": "abcd1234",
                "sub_session_id": "abcd1234",
                "target_agent_name": "Vera",
                "state": "running",
            }

    def fake_post(url, json=None, headers=None, timeout=None):  # noqa: ARG001
        posted["url"] = url
        posted["body"] = json
        posted["headers"] = headers
        return FakeResp()

    import httpx

    monkeypatch.setattr(httpx, "post", fake_post)

    out = _call(
        "ask_agent",
        name="vera",
        request="review the dashboard",
        files=["a.tsx", "b.tsx"],
    )
    # Request shape goes to /sessions/{sid}/delegations under the
    # OCTOPUS_SESSION_ID env, with auth header.
    assert posted["url"].endswith("/api/sessions/s/delegations")
    assert posted["body"] == {
        "agent_name": "vera",
        "request": "review the dashboard",
        "files": ["a.tsx", "b.tsx"],
    }
    assert posted["headers"]["Authorization"] == "Bearer t"
    # Tool's return text quotes the delegation id and target name so
    # the model can cite them back to the user.
    assert "abcd1234" in out
    assert "Vera" in out
    assert "agent-reply:Vera" in out


def test_ask_agent_omits_files_when_none(monkeypatch):
    monkeypatch.setenv("OCTOPUS_API_BASE", "http://x")
    monkeypatch.setenv("OCTOPUS_SESSION_ID", "s")
    monkeypatch.setenv("OCTOPUS_AUTH_TOKEN", "t")
    posted: dict = {}

    class FakeResp:
        status_code = 201
        text = ""

        def json(self):
            return {"delegation_id": "id", "target_agent_name": "Vera"}

    def fake_post(url, json=None, headers=None, timeout=None):  # noqa: ARG001
        posted["body"] = json
        return FakeResp()

    import httpx

    monkeypatch.setattr(httpx, "post", fake_post)
    _call("ask_agent", name="Vera", request="r")
    # `files` key is omitted entirely when the caller didn't pass any —
    # cleaner over the wire than an empty list.
    assert "files" not in posted["body"]


def test_ask_agent_404_passes_through(monkeypatch):
    monkeypatch.setenv("OCTOPUS_API_BASE", "http://x")
    monkeypatch.setenv("OCTOPUS_SESSION_ID", "s")
    monkeypatch.setenv("OCTOPUS_AUTH_TOKEN", "t")

    class R:
        status_code = 404
        text = "No agent named 'ghost'"

        def json(self):
            return {}

    import httpx

    monkeypatch.setattr(httpx, "post", lambda *a, **k: R())
    out = _call("ask_agent", name="ghost", request="r")
    assert "ghost" in out.lower()


def test_ask_agent_409_surfaced_with_reason(monkeypatch):
    """Cycle/depth/self errors come back as 409 with a server-side
    explanation — the tool should pass the reason through so the
    model can decide what to do next."""
    monkeypatch.setenv("OCTOPUS_API_BASE", "http://x")
    monkeypatch.setenv("OCTOPUS_SESSION_ID", "s")
    monkeypatch.setenv("OCTOPUS_AUTH_TOKEN", "t")

    class R:
        status_code = 409
        text = "Cycle rejected: target agent already in the caller chain"

        def json(self):
            return {}

    import httpx

    monkeypatch.setattr(httpx, "post", lambda *a, **k: R())
    out = _call("ask_agent", name="Octo", request="r")
    assert "cycle" in out.lower()


def test_ask_agent_http_error(monkeypatch):
    monkeypatch.setenv("OCTOPUS_API_BASE", "http://x")
    monkeypatch.setenv("OCTOPUS_SESSION_ID", "s")
    monkeypatch.setenv("OCTOPUS_AUTH_TOKEN", "t")

    import httpx

    def boom(*a, **k):  # noqa: ARG001
        raise httpx.ConnectError("nope")

    monkeypatch.setattr(httpx, "post", boom)
    out = _call("ask_agent", name="Vera", request="r")
    assert "failed to reach Octopus" in out


# ---------------------------------------------------------------------------
# cancel_agent_task
# ---------------------------------------------------------------------------


def test_cancel_agent_task_404(monkeypatch):
    monkeypatch.setenv("OCTOPUS_API_BASE", "http://x")
    monkeypatch.setenv("OCTOPUS_SESSION_ID", "s")
    monkeypatch.setenv("OCTOPUS_AUTH_TOKEN", "t")

    class R:
        status_code = 404
        text = ""

    import httpx

    monkeypatch.setattr(httpx, "post", lambda *a, **k: R())
    out = _call("cancel_agent_task", delegation_id="d1")
    assert "No delegation" in out


def test_cancel_agent_task_success(monkeypatch):
    monkeypatch.setenv("OCTOPUS_API_BASE", "http://x")
    monkeypatch.setenv("OCTOPUS_SESSION_ID", "s")
    monkeypatch.setenv("OCTOPUS_AUTH_TOKEN", "t")
    posted: dict = {}

    class R:
        status_code = 200

        def json(self):
            return {"state": "cancelled"}

    def fake_post(url, json=None, headers=None, timeout=None):  # noqa: ARG001
        posted["url"] = url
        posted["body"] = json
        return R()

    import httpx

    monkeypatch.setattr(httpx, "post", fake_post)
    out = _call(
        "cancel_agent_task", delegation_id="d1", reason="user stopped"
    )
    assert posted["url"].endswith(
        "/api/sessions/s/delegations/d1/cancel"
    )
    assert posted["body"] == {"reason": "user stopped"}
    assert "cancelled" in out


# ---------------------------------------------------------------------------
# list_agent_tasks
# ---------------------------------------------------------------------------


def test_list_agent_tasks_empty(monkeypatch):
    monkeypatch.setenv("OCTOPUS_API_BASE", "http://x")
    monkeypatch.setenv("OCTOPUS_SESSION_ID", "s")
    monkeypatch.setenv("OCTOPUS_AUTH_TOKEN", "t")

    class R:
        status_code = 200

        def json(self):
            return []

    import httpx

    monkeypatch.setattr(httpx, "get", lambda *a, **k: R())
    out = _call("list_agent_tasks")
    assert "No delegations" in out


# ---------------------------------------------------------------------------
# follow_up_agent
# ---------------------------------------------------------------------------


def test_follow_up_misconfigured(monkeypatch):
    monkeypatch.delenv("OCTOPUS_API_BASE", raising=False)
    monkeypatch.delenv("OCTOPUS_SESSION_ID", raising=False)
    monkeypatch.delenv("OCTOPUS_AUTH_TOKEN", raising=False)
    out = _call("follow_up_agent", delegation_id="d1", request="round 2")
    assert "misconfigured" in out.lower()


def test_follow_up_rejects_empty_args(monkeypatch):
    monkeypatch.setenv("OCTOPUS_API_BASE", "http://x")
    monkeypatch.setenv("OCTOPUS_SESSION_ID", "s")
    monkeypatch.setenv("OCTOPUS_AUTH_TOKEN", "t")
    assert "non-empty" in _call(
        "follow_up_agent", delegation_id="   ", request="round 2"
    )
    assert "non-empty" in _call(
        "follow_up_agent", delegation_id="d1", request="   "
    )


def test_follow_up_success(monkeypatch):
    monkeypatch.setenv("OCTOPUS_API_BASE", "http://x")
    monkeypatch.setenv("OCTOPUS_SESSION_ID", "s")
    monkeypatch.setenv("OCTOPUS_AUTH_TOKEN", "t")
    posted: dict = {}

    class R:
        status_code = 201

        def json(self):
            return {
                "delegation_id": "d1",
                "target_agent_name": "Vera",
                "state": "running",
            }

    def fake_post(url, json=None, headers=None, timeout=None):  # noqa: ARG001
        posted["url"] = url
        posted["body"] = json
        return R()

    import httpx

    monkeypatch.setattr(httpx, "post", fake_post)
    out = _call(
        "follow_up_agent",
        delegation_id="d1",
        request="round 2",
    )
    assert posted["url"].endswith(
        "/api/sessions/s/delegations/d1/follow-up"
    )
    assert posted["body"] == {"request": "round 2"}
    assert "Continued delegation" in out
    assert "Vera" in out
    # The follow-up text guides the model to end its turn (same
    # pattern as bg_run / ask_agent).
    assert "follow-up turn" in out
    assert "agent-reply:Vera" in out


def test_follow_up_404(monkeypatch):
    monkeypatch.setenv("OCTOPUS_API_BASE", "http://x")
    monkeypatch.setenv("OCTOPUS_SESSION_ID", "s")
    monkeypatch.setenv("OCTOPUS_AUTH_TOKEN", "t")

    class R:
        status_code = 404
        text = ""

    import httpx

    monkeypatch.setattr(httpx, "post", lambda *a, **k: R())
    out = _call(
        "follow_up_agent", delegation_id="ghost", request="r"
    )
    assert "No delegation" in out
    assert "ask_agent" in out  # nudges the model to start fresh


def test_follow_up_409_still_running(monkeypatch):
    monkeypatch.setenv("OCTOPUS_API_BASE", "http://x")
    monkeypatch.setenv("OCTOPUS_SESSION_ID", "s")
    monkeypatch.setenv("OCTOPUS_AUTH_TOKEN", "t")

    class R:
        status_code = 409
        text = "Delegation 'd1' is still running; wait for its reply"

    import httpx

    monkeypatch.setattr(httpx, "post", lambda *a, **k: R())
    out = _call(
        "follow_up_agent", delegation_id="d1", request="round 2"
    )
    assert "Cannot follow up" in out
    assert "still running" in out


# ---------------------------------------------------------------------------
# answer_agent_question
# ---------------------------------------------------------------------------


def test_answer_agent_question_misconfigured(monkeypatch):
    monkeypatch.delenv("OCTOPUS_API_BASE", raising=False)
    monkeypatch.delenv("OCTOPUS_SESSION_ID", raising=False)
    monkeypatch.delenv("OCTOPUS_AUTH_TOKEN", raising=False)
    out = _call(
        "answer_agent_question", delegation_id="d1", choice="A"
    )
    assert "misconfigured" in out.lower()


def test_answer_agent_question_rejects_empty(monkeypatch):
    monkeypatch.setenv("OCTOPUS_API_BASE", "http://x")
    monkeypatch.setenv("OCTOPUS_SESSION_ID", "s")
    monkeypatch.setenv("OCTOPUS_AUTH_TOKEN", "t")
    assert "non-empty" in _call(
        "answer_agent_question", delegation_id="   ", choice="A"
    )
    assert "non-empty" in _call(
        "answer_agent_question", delegation_id="d1", choice="   "
    )


def test_answer_agent_question_success(monkeypatch):
    monkeypatch.setenv("OCTOPUS_API_BASE", "http://x")
    monkeypatch.setenv("OCTOPUS_SESSION_ID", "s")
    monkeypatch.setenv("OCTOPUS_AUTH_TOKEN", "t")
    posted: dict = {}

    class R:
        status_code = 200

        def json(self):
            return {"ok": True}

    def fake_post(url, json=None, headers=None, timeout=None):  # noqa: ARG001
        posted["url"] = url
        posted["body"] = json
        return R()

    import httpx

    monkeypatch.setattr(httpx, "post", fake_post)
    out = _call(
        "answer_agent_question", delegation_id="d1", choice="Yes"
    )
    assert posted["url"].endswith(
        "/api/sessions/s/delegations/d1/answer"
    )
    assert posted["body"] == {"choice": "Yes"}
    assert "Answered" in out
    assert "Yes" in out


def test_answer_agent_question_404(monkeypatch):
    monkeypatch.setenv("OCTOPUS_API_BASE", "http://x")
    monkeypatch.setenv("OCTOPUS_SESSION_ID", "s")
    monkeypatch.setenv("OCTOPUS_AUTH_TOKEN", "t")

    class R:
        status_code = 404
        text = ""

    import httpx

    monkeypatch.setattr(httpx, "post", lambda *a, **k: R())
    out = _call(
        "answer_agent_question", delegation_id="d-gone", choice="A"
    )
    assert "No delegation" in out


def test_answer_agent_question_409(monkeypatch):
    """No pending question / human raced: 409 with reason text passes
    through so the parent's model can choose to give up or retry."""
    monkeypatch.setenv("OCTOPUS_API_BASE", "http://x")
    monkeypatch.setenv("OCTOPUS_SESSION_ID", "s")
    monkeypatch.setenv("OCTOPUS_AUTH_TOKEN", "t")

    class R:
        status_code = 409
        text = "Question already answered by another path"

    import httpx

    monkeypatch.setattr(httpx, "post", lambda *a, **k: R())
    out = _call(
        "answer_agent_question", delegation_id="d1", choice="A"
    )
    assert "Cannot answer" in out
    assert "already answered" in out


def test_list_agent_tasks_summary(monkeypatch):
    monkeypatch.setenv("OCTOPUS_API_BASE", "http://x")
    monkeypatch.setenv("OCTOPUS_SESSION_ID", "s")
    monkeypatch.setenv("OCTOPUS_AUTH_TOKEN", "t")

    class R:
        status_code = 200

        def json(self):
            return [
                {
                    "delegation_id": "d1",
                    "target_agent_name": "Vera",
                    "state": "running",
                    "error": None,
                },
                {
                    "delegation_id": "d2",
                    "target_agent_name": "Pete",
                    "state": "failed",
                    "error": "boom",
                },
            ]

    import httpx

    monkeypatch.setattr(httpx, "get", lambda *a, **k: R())
    out = _call("list_agent_tasks")
    assert "d1" in out and "Vera" in out and "running" in out
    assert "d2" in out and "Pete" in out and "failed" in out
    assert "boom" in out
