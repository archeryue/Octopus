"""Tests for the pure-Python PKCE OAuth login orchestrator.

No subprocesses, no PTY — the orchestrator now talks directly to the
Anthropic OAuth endpoints over HTTP. Tests mock httpx so they run
offline.
"""

from __future__ import annotations

import pytest

from server import oauth_login as ol
from server.oauth_login import (
    AUTHORIZE_URL,
    CLIENT_ID,
    LoginState,
    OAuthLoginManager,
    SCOPES,
    _b64url,
    _challenge_from,
    _gen_state,
    _gen_verifier,
    _split_code,
)


# ---------------------------------------------------------------------------
# PKCE helpers
# ---------------------------------------------------------------------------


def test_verifier_is_b64url_no_padding():
    v = _gen_verifier()
    assert "=" not in v
    assert "+" not in v
    assert "/" not in v
    # 32 random bytes → 43-char base64url (no padding)
    assert len(v) == 43


def test_challenge_is_b64url_sha256_of_verifier():
    # Known-vector check against the RFC 7636 method.
    import hashlib
    verifier = "test-verifier-known"
    expected = _b64url(hashlib.sha256(verifier.encode()).digest())
    assert _challenge_from(verifier) == expected


def test_state_is_random_per_call():
    assert _gen_state() != _gen_state()


def test_split_code_handles_both_formats():
    # The Anthropic callback page formats it as `<code>#<state>`. We accept
    # the bare code too in case the user trimmed it.
    assert _split_code("ABC#STATE_X") == ("ABC", "STATE_X")
    assert _split_code("  ABC#STATE_X  ") == ("ABC", "STATE_X")
    assert _split_code("ABC") == ("ABC", None)
    assert _split_code("ABC#") == ("ABC", None)


# ---------------------------------------------------------------------------
# start() builds a correct authorize URL
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_returns_authorize_url_with_pkce_params():
    mgr = OAuthLoginManager()
    session = await mgr.start()
    assert session.state == LoginState.awaiting_code

    assert session.url.startswith(AUTHORIZE_URL)
    assert f"client_id={CLIENT_ID}" in session.url
    assert "code_challenge=" in session.url
    assert "code_challenge_method=S256" in session.url
    assert "response_type=code" in session.url
    # Scopes are URL-encoded with `+` (urlencode default), so check decoded
    from urllib.parse import urlparse, parse_qs
    parts = parse_qs(urlparse(session.url).query)
    assert parts["scope"][0].split() == SCOPES
    assert parts["redirect_uri"][0].endswith("/oauth/code/callback")

    # Verifier+state were stored
    assert session._verifier
    assert session._state


@pytest.mark.asyncio
async def test_two_starts_get_independent_verifiers():
    mgr = OAuthLoginManager()
    s1 = await mgr.start()
    s2 = await mgr.start()
    assert s1.id != s2.id
    assert s1._verifier != s2._verifier
    assert s1._state != s2._state


# ---------------------------------------------------------------------------
# submit_code: happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_code_happy_path(monkeypatch):
    """code → token endpoint → access_token → api-key endpoint → sk-ant- key."""
    mgr = OAuthLoginManager()
    session = await mgr.start()

    calls: list[tuple[str, dict]] = []

    async def fake_exchange(*, code, code_verifier, state):
        calls.append(("exchange", {"code": code, "verifier": code_verifier, "state": state}))
        assert code == "the-code"
        assert code_verifier == session._verifier
        assert state == session._state
        return "oauth-access-token-xyz"

    async def fake_api_key(access_token):
        calls.append(("api_key", {"access_token": access_token}))
        assert access_token == "oauth-access-token-xyz"
        return "sk-ant-real-1234567890"

    monkeypatch.setattr(ol, "_exchange_code_for_access_token", fake_exchange)
    monkeypatch.setattr(ol, "_create_long_lived_api_key", fake_api_key)

    pasted = f"the-code#{session._state}"
    finished = await mgr.submit_code(session.id, pasted)

    assert finished.state == LoginState.success
    assert finished.token == "sk-ant-real-1234567890"
    assert [c[0] for c in calls] == ["exchange", "api_key"]


@pytest.mark.asyncio
async def test_submit_code_accepts_bare_code_without_state(monkeypatch):
    """If the user only pasted the code half (no #state), it still works
    but we skip the state check."""
    mgr = OAuthLoginManager()
    session = await mgr.start()

    async def fake_exchange(*, code, code_verifier, state):
        return "tok"

    async def fake_api_key(access_token):
        return "sk-ant-fine-abc"

    monkeypatch.setattr(ol, "_exchange_code_for_access_token", fake_exchange)
    monkeypatch.setattr(ol, "_create_long_lived_api_key", fake_api_key)

    s = await mgr.submit_code(session.id, "just-the-code")
    assert s.state == LoginState.success


# ---------------------------------------------------------------------------
# submit_code: error paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_code_wrong_state_rejected(monkeypatch):
    """Mismatched state must abort before any network call."""
    mgr = OAuthLoginManager()
    session = await mgr.start()

    called = False

    async def fake_exchange(*, code, code_verifier, state):
        nonlocal called
        called = True
        return "tok"

    monkeypatch.setattr(ol, "_exchange_code_for_access_token", fake_exchange)

    with pytest.raises(RuntimeError, match="state mismatch"):
        await mgr.submit_code(session.id, "the-code#WRONG-STATE")
    assert called is False
    assert mgr.get(session.id).state == LoginState.error


@pytest.mark.asyncio
async def test_submit_code_token_endpoint_failure(monkeypatch):
    mgr = OAuthLoginManager()
    session = await mgr.start()

    async def fake_exchange(*, code, code_verifier, state):
        raise RuntimeError("token endpoint returned 400: invalid_grant")

    monkeypatch.setattr(ol, "_exchange_code_for_access_token", fake_exchange)

    with pytest.raises(RuntimeError, match="token exchange failed"):
        await mgr.submit_code(session.id, f"x#{session._state}")
    assert mgr.get(session.id).state == LoginState.error


@pytest.mark.asyncio
async def test_submit_code_api_key_endpoint_failure(monkeypatch):
    mgr = OAuthLoginManager()
    session = await mgr.start()

    async def fake_exchange(*, code, code_verifier, state):
        return "good-access-token"

    async def fake_api_key(access_token):
        raise RuntimeError("api-key endpoint returned 500: oops")

    monkeypatch.setattr(ol, "_exchange_code_for_access_token", fake_exchange)
    monkeypatch.setattr(ol, "_create_long_lived_api_key", fake_api_key)

    with pytest.raises(RuntimeError, match="API key creation failed"):
        await mgr.submit_code(session.id, f"x#{session._state}")
    assert mgr.get(session.id).state == LoginState.error


@pytest.mark.asyncio
async def test_submit_code_unknown_id_raises():
    mgr = OAuthLoginManager()
    with pytest.raises(KeyError):
        await mgr.submit_code("nope", "x")


@pytest.mark.asyncio
async def test_submit_code_wrong_state_raises_on_completed_session(monkeypatch):
    mgr = OAuthLoginManager()
    session = await mgr.start()

    async def fake_exchange(*, code, code_verifier, state):
        return "tok"

    async def fake_api_key(access_token):
        return "sk-ant-zzz-abc"

    monkeypatch.setattr(ol, "_exchange_code_for_access_token", fake_exchange)
    monkeypatch.setattr(ol, "_create_long_lived_api_key", fake_api_key)

    await mgr.submit_code(session.id, f"c#{session._state}")
    with pytest.raises(RuntimeError, match="cannot accept code"):
        await mgr.submit_code(session.id, f"c2#{session._state}")


# ---------------------------------------------------------------------------
# cancel + shutdown
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_is_idempotent():
    mgr = OAuthLoginManager()
    s = await mgr.start()
    await mgr.cancel(s.id)
    assert mgr.get(s.id).state == LoginState.cancelled
    await mgr.cancel(s.id)  # idempotent


@pytest.mark.asyncio
async def test_shutdown_clears_state():
    mgr = OAuthLoginManager()
    await mgr.start()
    await mgr.start()
    assert len(mgr._sessions) == 2
    await mgr.shutdown()
    assert mgr._sessions == {}


# ---------------------------------------------------------------------------
# HTTP helpers (mock httpx)
# ---------------------------------------------------------------------------


class _StubResponse:
    def __init__(self, status_code, json_body=None, text_body=""):
        self.status_code = status_code
        self._json_body = json_body
        self.text = text_body if json_body is None else "<json>"

    def json(self):
        if self._json_body is None:
            raise ValueError("no json body")
        return self._json_body


class _StubAsyncClient:
    def __init__(self, *, post_handler):
        self._post_handler = post_handler

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, **kwargs):
        return await self._post_handler(url, **kwargs)


def _install_stub_client(monkeypatch, post_handler):
    """Replace httpx.AsyncClient with one whose .post() runs post_handler."""
    monkeypatch.setattr(
        ol.httpx,
        "AsyncClient",
        lambda *a, **kw: _StubAsyncClient(post_handler=post_handler),
    )


@pytest.mark.asyncio
async def test_exchange_returns_access_token(monkeypatch):
    async def handler(url, **kwargs):
        assert url == ol.TOKEN_URL
        payload = kwargs["json"]
        assert payload["grant_type"] == "authorization_code"
        assert payload["code"] == "c"
        assert payload["code_verifier"] == "v"
        return _StubResponse(200, json_body={"access_token": "the-token", "scope": "x"})

    _install_stub_client(monkeypatch, handler)
    tok = await ol._exchange_code_for_access_token(code="c", code_verifier="v", state="s")
    assert tok == "the-token"


@pytest.mark.asyncio
async def test_exchange_non_200_raises(monkeypatch):
    async def handler(url, **kwargs):
        return _StubResponse(400, text_body="invalid_grant")

    _install_stub_client(monkeypatch, handler)
    with pytest.raises(RuntimeError, match="token endpoint returned 400"):
        await ol._exchange_code_for_access_token(code="c", code_verifier="v", state="s")


@pytest.mark.asyncio
async def test_exchange_missing_access_token_raises(monkeypatch):
    async def handler(url, **kwargs):
        return _StubResponse(200, json_body={"other": "field"})

    _install_stub_client(monkeypatch, handler)
    with pytest.raises(RuntimeError, match="missing access_token"):
        await ol._exchange_code_for_access_token(code="c", code_verifier="v", state="s")


@pytest.mark.asyncio
async def test_create_api_key_returns_raw_key(monkeypatch):
    async def handler(url, **kwargs):
        assert url == ol.API_KEY_URL
        headers = kwargs["headers"]
        assert headers["Authorization"] == "Bearer T"
        return _StubResponse(200, json_body={"raw_key": "sk-ant-real-zzz", "label": "x"})

    _install_stub_client(monkeypatch, handler)
    key = await ol._create_long_lived_api_key("T")
    assert key == "sk-ant-real-zzz"


@pytest.mark.asyncio
async def test_create_api_key_accepts_alternate_field_names(monkeypatch):
    """If Anthropic renames the field, we still pick it up as long as
    the value starts with sk-ant-."""
    async def handler(url, **kwargs):
        return _StubResponse(200, json_body={"api_key": "sk-ant-via-api_key"})

    _install_stub_client(monkeypatch, handler)
    key = await ol._create_long_lived_api_key("T")
    assert key == "sk-ant-via-api_key"


@pytest.mark.asyncio
async def test_create_api_key_no_sk_ant_raises(monkeypatch):
    async def handler(url, **kwargs):
        return _StubResponse(200, json_body={"raw_key": "not-an-ant-key"})

    _install_stub_client(monkeypatch, handler)
    with pytest.raises(RuntimeError, match="didn't include a sk-ant"):
        await ol._create_long_lived_api_key("T")


@pytest.mark.asyncio
async def test_create_api_key_non_200_raises(monkeypatch):
    async def handler(url, **kwargs):
        return _StubResponse(403, text_body="forbidden")

    _install_stub_client(monkeypatch, handler)
    with pytest.raises(RuntimeError, match="api-key endpoint returned 403"):
        await ol._create_long_lived_api_key("T")
