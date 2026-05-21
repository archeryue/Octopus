"""Connector OAuth plumbing (connectors.md §5.5.5): PKCE/state helpers and the
in-memory pending-login manager (CSRF validation + TTL)."""

from __future__ import annotations

import base64
import hashlib

import pytest

from server.connectors.oauth import (
    ConnectorLoginError,
    ConnectorLoginManager,
    ConnectorLoginState,
    challenge_from,
    gen_state,
    gen_verifier,
)
from server.oauth_providers import OAuthTokenSet


class FakeProvider:
    """Structural ConnectorOAuthProvider for tests."""

    kind = "fake"
    authorize_url = "https://fake.example/authorize"
    token_url = "https://fake.example/token"
    default_scopes = ["scope.a"]
    pkce = True

    def build_authorize_url(self, *, client_id, redirect_uri, code_challenge, state):
        return (
            f"{self.authorize_url}?client_id={client_id}&redirect_uri={redirect_uri}"
            f"&code_challenge={code_challenge or ''}&state={state}"
        )

    async def exchange_code(
        self, *, client_id, client_secret, code, redirect_uri, code_verifier, state
    ):
        return OAuthTokenSet(
            access_token="at", refresh_token="rt", expires_at_epoch=0.0
        )

    async def refresh(self, *, client_id, client_secret, refresh_token):
        return OAuthTokenSet(
            access_token="at2", refresh_token=refresh_token, expires_at_epoch=0.0
        )


class NoPkceProvider(FakeProvider):
    kind = "nopkce"
    pkce = False


# --- helpers ---------------------------------------------------------------


def test_challenge_is_deterministic_and_correct():
    v = "test-verifier"
    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    assert challenge_from(v) == expected
    assert challenge_from(v) == challenge_from(v)


def test_verifier_and_state_are_random_and_urlsafe():
    a, b = gen_verifier(), gen_verifier()
    assert a != b
    assert gen_state() != gen_state()
    # url-safe, unpadded
    for s in (a, gen_state()):
        assert "=" not in s and "+" not in s and "/" not in s


# --- login manager ---------------------------------------------------------


def test_start_builds_authorize_url_with_state_and_challenge():
    mgr = ConnectorLoginManager()
    pl = mgr.start(provider=FakeProvider(), client_id="cid", redirect_uri="https://app/cb")
    assert pl.kind == "fake"
    assert pl.verifier is not None
    # The composite state login_id:raw_state is carried through the provider.
    assert f"state={pl.login_id}:{pl.state}" in pl.authorize_url
    assert "code_challenge=" in pl.authorize_url
    assert challenge_from(pl.verifier) in pl.authorize_url
    assert mgr.get(pl.login_id) is pl


def test_start_without_pkce_has_no_verifier():
    mgr = ConnectorLoginManager()
    pl = mgr.start(provider=NoPkceProvider(), client_id="cid", redirect_uri="https://app/cb")
    assert pl.verifier is None
    # challenge segment is empty
    assert "code_challenge=&" in pl.authorize_url


def test_resolve_callback_success():
    mgr = ConnectorLoginManager()
    pl = mgr.start(provider=FakeProvider(), client_id="cid", redirect_uri="https://app/cb")
    resolved = mgr.resolve_callback(f"{pl.login_id}:{pl.state}")
    assert resolved is pl


def test_resolve_callback_rejects_bad_state():
    mgr = ConnectorLoginManager()
    pl = mgr.start(provider=FakeProvider(), client_id="cid", redirect_uri="https://app/cb")
    with pytest.raises(ConnectorLoginError):
        mgr.resolve_callback(f"{pl.login_id}:tampered")


def test_resolve_callback_rejects_unknown_login():
    mgr = ConnectorLoginManager()
    with pytest.raises(ConnectorLoginError):
        mgr.resolve_callback("nope:whatever")


def test_status_transitions():
    mgr = ConnectorLoginManager()
    pl = mgr.start(provider=FakeProvider(), client_id="cid", redirect_uri="https://app/cb")
    assert pl.status == ConnectorLoginState.pending
    mgr.mark_success(pl.login_id, "inst-1")
    assert pl.status == ConnectorLoginState.success
    assert pl.installation_id == "inst-1"

    pl2 = mgr.start(provider=FakeProvider(), client_id="cid", redirect_uri="https://app/cb")
    mgr.mark_error(pl2.login_id, "boom")
    assert pl2.status == ConnectorLoginState.error and pl2.message == "boom"

    pl3 = mgr.start(provider=FakeProvider(), client_id="cid", redirect_uri="https://app/cb")
    assert mgr.cancel(pl3.login_id) is True
    assert pl3.status == ConnectorLoginState.cancelled
    assert mgr.cancel("ghost") is False


def test_expired_logins_are_gc_d():
    mgr = ConnectorLoginManager()
    pl = mgr.start(provider=FakeProvider(), client_id="cid", redirect_uri="https://app/cb")
    # Age it past the TTL, then trigger _gc via another start().
    pl.created_at -= 10_000
    mgr.start(provider=FakeProvider(), client_id="cid", redirect_uri="https://app/cb")
    assert mgr.get(pl.login_id) is None
