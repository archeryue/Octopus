"""Tests for the PKCE OAuth login orchestrator + provider abstraction.

The orchestrator (`OAuthLoginManager`) is provider-agnostic; the
Claude-specific HTTP calls live in `server.oauth_providers.ClaudeCodeProvider`.
Tests are split:

  - PKCE/state helpers (pure)
  - start() against the registry
  - submit_code() with provider methods monkeypatched (api-key + oauth paths)
  - ClaudeCodeProvider HTTP helpers with httpx stubbed
  - Provider registry + module-level back-compat shims
"""

from __future__ import annotations

import time

import pytest

from server import oauth_login as ol
from server import oauth_providers as op
from server.oauth_errors import ScopeMissingError
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
from server.oauth_providers import OAuthTokenSet


def _fresh_token_set(
    access_token: str = "oauth-access-token-xyz",
    refresh_token: str | None = "oauth-refresh-token-zzz",
    scopes: list[str] | None = None,
    expires_in: float = 3600,
) -> OAuthTokenSet:
    return OAuthTokenSet(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at_epoch=time.time() + expires_in,
        scopes=scopes if scopes is not None else ["org:create_api_key", "user:profile"],
    )


# ---------------------------------------------------------------------------
# PKCE helpers
# ---------------------------------------------------------------------------


def test_verifier_is_b64url_no_padding():
    v = _gen_verifier()
    assert "=" not in v
    assert "+" not in v
    assert "/" not in v
    assert len(v) == 43  # 32 random bytes → 43-char base64url (no padding)


def test_challenge_is_b64url_sha256_of_verifier():
    import hashlib
    verifier = "test-verifier-known"
    expected = _b64url(hashlib.sha256(verifier.encode()).digest())
    assert _challenge_from(verifier) == expected


def test_state_is_random_per_call():
    assert _gen_state() != _gen_state()


def test_split_code_handles_both_formats():
    assert _split_code("ABC#STATE_X") == ("ABC", "STATE_X")
    assert _split_code("  ABC#STATE_X  ") == ("ABC", "STATE_X")
    assert _split_code("ABC") == ("ABC", None)
    assert _split_code("ABC#") == ("ABC", None)


# ---------------------------------------------------------------------------
# start() — uses provider registry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_returns_authorize_url_with_pkce_params():
    mgr = OAuthLoginManager()
    session = await mgr.start()
    assert session.state == LoginState.awaiting_code
    assert session.provider_name == "claude-code"

    assert session.url.startswith(AUTHORIZE_URL)
    assert f"client_id={CLIENT_ID}" in session.url
    assert "code_challenge=" in session.url
    assert "code_challenge_method=S256" in session.url
    assert "response_type=code" in session.url

    from urllib.parse import urlparse, parse_qs
    parts = parse_qs(urlparse(session.url).query)
    assert parts["scope"][0].split() == SCOPES
    assert parts["redirect_uri"][0].endswith("/oauth/code/callback")

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


@pytest.mark.asyncio
async def test_start_unknown_provider_raises():
    mgr = OAuthLoginManager()
    with pytest.raises(KeyError, match="unknown OAuth provider"):
        await mgr.start("not-a-provider")


# ---------------------------------------------------------------------------
# submit_code: happy path
# ---------------------------------------------------------------------------


def _patch_claude_provider(monkeypatch, *, exchange=None, mint=None, refresh=None):
    """Replace the Claude provider's HTTP-calling methods with stubs.

    Restores the originals on teardown via monkeypatch."""
    provider = op.get_provider("claude-code")
    if exchange is not None:
        monkeypatch.setattr(provider, "exchange_code", exchange)
    if mint is not None:
        monkeypatch.setattr(provider, "mint_api_key", mint)
    if refresh is not None:
        monkeypatch.setattr(provider, "refresh_access_token", refresh)


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
        return _fresh_token_set(access_token="oauth-access-token-xyz")

    async def fake_mint(access_token):
        calls.append(("mint", {"access_token": access_token}))
        assert access_token == "oauth-access-token-xyz"
        return "sk-ant-real-1234567890"

    _patch_claude_provider(monkeypatch, exchange=fake_exchange, mint=fake_mint)

    pasted = f"the-code#{session._state}"
    finished = await mgr.submit_code(session.id, pasted)

    assert finished.state == LoginState.success
    assert finished.token == "sk-ant-real-1234567890"
    assert finished.oauth_tokens is None
    assert [c[0] for c in calls] == ["exchange", "mint"]


@pytest.mark.asyncio
async def test_submit_code_accepts_bare_code_without_state(monkeypatch):
    """If the user only pasted the code half (no #state), it still works
    but we skip the state check."""
    mgr = OAuthLoginManager()
    session = await mgr.start()

    async def fake_exchange(*, code, code_verifier, state):
        return _fresh_token_set(access_token="tok")

    async def fake_mint(access_token):
        return "sk-ant-fine-abc"

    _patch_claude_provider(monkeypatch, exchange=fake_exchange, mint=fake_mint)

    s = await mgr.submit_code(session.id, "just-the-code")
    assert s.state == LoginState.success


@pytest.mark.asyncio
async def test_submit_code_falls_back_to_oauth_when_scope_missing(monkeypatch):
    """Pro/Max account: mint_api_key raises ScopeMissingError → store
    the OAuthTokenSet instead of an API key."""
    mgr = OAuthLoginManager()
    session = await mgr.start()

    ts = _fresh_token_set(
        access_token="sk-ant-oat01-personal",
        refresh_token="sk-ant-ort01-personal",
        scopes=["user:inference", "user:profile"],
    )

    async def fake_exchange(*, code, code_verifier, state):
        return ts

    async def fake_mint(access_token):
        raise ScopeMissingError(
            "api-key endpoint returned 403: scope error",
            missing_scope="org:create_api_key",
        )

    _patch_claude_provider(monkeypatch, exchange=fake_exchange, mint=fake_mint)

    finished = await mgr.submit_code(session.id, f"c#{session._state}")
    assert finished.state == LoginState.success
    assert finished.token is None
    assert finished.oauth_tokens is ts
    assert finished.oauth_tokens.refresh_token == "sk-ant-ort01-personal"


@pytest.mark.asyncio
async def test_submit_code_scope_missing_without_refresh_token_errors(monkeypatch):
    """If we somehow get a scope error AND no refresh_token, we can't
    sustain auth — surface this as a login error (not silent success)."""
    mgr = OAuthLoginManager()
    session = await mgr.start()

    async def fake_exchange(*, code, code_verifier, state):
        return _fresh_token_set(refresh_token=None)

    async def fake_mint(access_token):
        raise ScopeMissingError("403 scope error", missing_scope="org:create_api_key")

    _patch_claude_provider(monkeypatch, exchange=fake_exchange, mint=fake_mint)

    with pytest.raises(RuntimeError, match="didn't include a refresh token"):
        await mgr.submit_code(session.id, f"c#{session._state}")
    assert mgr.get(session.id).state == LoginState.error


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

    _patch_claude_provider(monkeypatch, exchange=fake_exchange)

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

    _patch_claude_provider(monkeypatch, exchange=fake_exchange)

    with pytest.raises(RuntimeError, match="token exchange failed"):
        await mgr.submit_code(session.id, f"x#{session._state}")
    assert mgr.get(session.id).state == LoginState.error


@pytest.mark.asyncio
async def test_submit_code_api_key_endpoint_failure(monkeypatch):
    """Non-scope api-key failures still surface as login errors —
    only ScopeMissingError triggers the fallback."""
    mgr = OAuthLoginManager()
    session = await mgr.start()

    async def fake_exchange(*, code, code_verifier, state):
        return _fresh_token_set(access_token="good-access-token")

    async def fake_mint(access_token):
        raise RuntimeError("api-key endpoint returned 500: oops")

    _patch_claude_provider(monkeypatch, exchange=fake_exchange, mint=fake_mint)

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
        return _fresh_token_set(access_token="tok")

    async def fake_mint(access_token):
        return "sk-ant-zzz-abc"

    _patch_claude_provider(monkeypatch, exchange=fake_exchange, mint=fake_mint)

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
    await mgr.cancel(s.id)


@pytest.mark.asyncio
async def test_shutdown_clears_state():
    mgr = OAuthLoginManager()
    await mgr.start()
    await mgr.start()
    assert len(mgr._sessions) == 2
    await mgr.shutdown()
    assert mgr._sessions == {}


# ---------------------------------------------------------------------------
# ClaudeCodeProvider HTTP helpers (mock httpx)
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
    """Replace httpx.AsyncClient in oauth_providers with one whose
    .post() runs post_handler."""
    monkeypatch.setattr(
        op.httpx,
        "AsyncClient",
        lambda *a, **kw: _StubAsyncClient(post_handler=post_handler),
    )


@pytest.mark.asyncio
async def test_exchange_returns_token_set(monkeypatch):
    provider = op.get_provider("claude-code")

    async def handler(url, **kwargs):
        assert url == provider.TOKEN_URL  # type: ignore[attr-defined]
        payload = kwargs["json"]
        assert payload["grant_type"] == "authorization_code"
        assert payload["code"] == "c"
        assert payload["code_verifier"] == "v"
        return _StubResponse(
            200,
            json_body={
                "access_token": "the-token",
                "refresh_token": "the-refresh",
                "expires_in": 1800,
                "scope": "org:create_api_key user:profile",
                "token_type": "Bearer",
            },
        )

    _install_stub_client(monkeypatch, handler)
    ts = await provider.exchange_code(code="c", code_verifier="v", state="s")
    assert isinstance(ts, OAuthTokenSet)
    assert ts.access_token == "the-token"
    assert ts.refresh_token == "the-refresh"
    assert ts.scopes == ["org:create_api_key", "user:profile"]
    # expires_at_epoch is wall-clock-based; check it lands in a sane window.
    assert time.time() + 1500 < ts.expires_at_epoch < time.time() + 1900


@pytest.mark.asyncio
async def test_exchange_non_200_raises(monkeypatch):
    provider = op.get_provider("claude-code")

    async def handler(url, **kwargs):
        return _StubResponse(400, text_body="invalid_grant")

    _install_stub_client(monkeypatch, handler)
    with pytest.raises(RuntimeError, match="token endpoint returned 400"):
        await provider.exchange_code(code="c", code_verifier="v", state="s")


@pytest.mark.asyncio
async def test_exchange_missing_access_token_raises(monkeypatch):
    provider = op.get_provider("claude-code")

    async def handler(url, **kwargs):
        return _StubResponse(200, json_body={"other": "field"})

    _install_stub_client(monkeypatch, handler)
    with pytest.raises(RuntimeError, match="missing access_token"):
        await provider.exchange_code(code="c", code_verifier="v", state="s")


@pytest.mark.asyncio
async def test_refresh_access_token_happy_path(monkeypatch):
    provider = op.get_provider("claude-code")

    async def handler(url, **kwargs):
        assert url == provider.TOKEN_URL  # type: ignore[attr-defined]
        payload = kwargs["json"]
        assert payload["grant_type"] == "refresh_token"
        assert payload["refresh_token"] == "old-refresh"
        return _StubResponse(
            200,
            json_body={
                "access_token": "fresh-access",
                "refresh_token": "fresh-refresh",
                "expires_in": 3600,
                "scope": "user:inference user:profile",
            },
        )

    _install_stub_client(monkeypatch, handler)
    ts = await provider.refresh_access_token("old-refresh")
    assert ts.access_token == "fresh-access"
    assert ts.refresh_token == "fresh-refresh"


@pytest.mark.asyncio
async def test_refresh_access_token_reuses_old_refresh_when_omitted(monkeypatch):
    """Some OAuth servers don't reissue the refresh token on refresh —
    we should keep the old one rather than dropping to None."""
    provider = op.get_provider("claude-code")

    async def handler(url, **kwargs):
        return _StubResponse(
            200,
            json_body={
                "access_token": "fresh-access",
                # no refresh_token in the response
                "expires_in": 3600,
            },
        )

    _install_stub_client(monkeypatch, handler)
    ts = await provider.refresh_access_token("keep-this-refresh")
    assert ts.refresh_token == "keep-this-refresh"


@pytest.mark.asyncio
async def test_refresh_access_token_non_200_raises(monkeypatch):
    provider = op.get_provider("claude-code")

    async def handler(url, **kwargs):
        return _StubResponse(400, text_body="invalid_grant")

    _install_stub_client(monkeypatch, handler)
    with pytest.raises(RuntimeError, match="refresh endpoint returned 400"):
        await provider.refresh_access_token("doesnt-matter")


@pytest.mark.asyncio
async def test_create_api_key_returns_raw_key(monkeypatch):
    provider = op.get_provider("claude-code")

    async def handler(url, **kwargs):
        assert url == provider.API_KEY_URL  # type: ignore[attr-defined]
        headers = kwargs["headers"]
        assert headers["Authorization"] == "Bearer T"
        return _StubResponse(200, json_body={"raw_key": "sk-ant-real-zzz", "label": "x"})

    _install_stub_client(monkeypatch, handler)
    key = await provider.mint_api_key("T")
    assert key == "sk-ant-real-zzz"


@pytest.mark.asyncio
async def test_create_api_key_accepts_alternate_field_names(monkeypatch):
    """If Anthropic renames the field, we still pick it up as long as
    the value starts with sk-ant-."""
    provider = op.get_provider("claude-code")

    async def handler(url, **kwargs):
        return _StubResponse(200, json_body={"api_key": "sk-ant-via-api_key"})

    _install_stub_client(monkeypatch, handler)
    key = await provider.mint_api_key("T")
    assert key == "sk-ant-via-api_key"


@pytest.mark.asyncio
async def test_create_api_key_no_sk_ant_raises(monkeypatch):
    provider = op.get_provider("claude-code")

    async def handler(url, **kwargs):
        return _StubResponse(200, json_body={"raw_key": "not-an-ant-key"})

    _install_stub_client(monkeypatch, handler)
    with pytest.raises(RuntimeError, match="didn't include a sk-ant"):
        await provider.mint_api_key("T")


@pytest.mark.asyncio
async def test_create_api_key_non_200_raises(monkeypatch):
    """Non-403 errors and 403 errors without the scope marker stay as
    generic RuntimeError — only the scope-specific 403 escalates."""
    provider = op.get_provider("claude-code")

    async def handler(url, **kwargs):
        return _StubResponse(403, text_body="forbidden")

    _install_stub_client(monkeypatch, handler)
    with pytest.raises(RuntimeError, match="api-key endpoint returned 403"):
        await provider.mint_api_key("T")


@pytest.mark.asyncio
async def test_create_api_key_403_with_scope_text_raises_scope_missing(monkeypatch):
    """403 body that names the missing scope → ScopeMissingError so the
    orchestrator routes the user into the OAuth-token storage path."""
    provider = op.get_provider("claude-code")

    body = (
        '{"type":"error","error":{"type":"permission_error",'
        '"message":"OAuth token does not meet scope requirement '
        'org:create_api_key"}}'
    )

    async def handler(url, **kwargs):
        return _StubResponse(403, text_body=body)

    _install_stub_client(monkeypatch, handler)
    with pytest.raises(ScopeMissingError) as exc_info:
        await provider.mint_api_key("T")
    assert exc_info.value.missing_scope == "org:create_api_key"


# ---------------------------------------------------------------------------
# Provider registry
# ---------------------------------------------------------------------------


def test_registry_has_claude_code():
    provider = op.get_provider("claude-code")
    assert provider.name == "claude-code"


def test_registry_unknown_raises():
    with pytest.raises(KeyError, match="unknown OAuth provider"):
        op.get_provider("nope")


def test_module_level_back_compat_constants_match_provider():
    """The pre-refactor module-level constants are re-exported from the
    Claude provider — make sure they stay in sync."""
    provider = op.get_provider("claude-code")
    assert ol.CLIENT_ID == provider.CLIENT_ID  # type: ignore[attr-defined]
    assert ol.AUTHORIZE_URL == provider.AUTHORIZE_URL  # type: ignore[attr-defined]
    assert ol.SCOPES == list(provider.SCOPES)  # type: ignore[attr-defined]
