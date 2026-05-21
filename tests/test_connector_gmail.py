"""Gmail connector descriptor + OAuth provider (connectors.md Phase C / §6.2):
PKCE+offline authorize URL, token parse with expiry, the live refresh path,
and identity via the profile endpoint."""

from __future__ import annotations

import time

import pytest

from server.config import settings
from server.connector_manager import connector_available
from server.connectors import gmail as gm
from server.connectors.registry import get_connector


def _fake_client(body: dict, status: int = 200, capture: dict | None = None):
    class FakeResponse:
        status_code = status
        content = b"x"

        def json(self):
            return body

        def raise_for_status(self):
            if status >= 400:
                raise RuntimeError(f"HTTP {status}")

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, data=None, headers=None):
            if capture is not None:
                capture["url"], capture["data"] = url, data
            return FakeResponse()

        async def get(self, url, headers=None):
            if capture is not None:
                capture["url"], capture["headers"] = url, headers
            return FakeResponse()

    return FakeClient


def test_gmail_is_registered():
    assert isinstance(get_connector("gmail"), gm.GmailConnector)


def test_build_authorize_url_pkce_offline(monkeypatch):
    monkeypatch.setattr(settings, "gmail_oauth_client_id", "cid")
    url = gm.GmailOAuthProvider().build_authorize_url(
        redirect_uri="https://app/cb", code_challenge="chal", state="lid:rnd"
    )
    assert "client_id=cid" in url
    assert "access_type=offline" in url
    assert "prompt=consent" in url
    assert "code_challenge=chal" in url
    assert "code_challenge_method=S256" in url
    assert "gmail.modify" in url


def test_availability(monkeypatch):
    monkeypatch.setattr(settings, "gmail_oauth_client_id", "cid")
    monkeypatch.setattr(settings, "gmail_oauth_client_secret", "csec")
    assert connector_available(gm.GmailConnector()) is True
    monkeypatch.setattr(settings, "gmail_oauth_client_secret", None)
    assert connector_available(gm.GmailConnector()) is False


@pytest.mark.asyncio
async def test_exchange_code_parses_expiry(monkeypatch):
    monkeypatch.setattr(settings, "gmail_oauth_client_id", "cid")
    monkeypatch.setattr(settings, "gmail_oauth_client_secret", "csec")
    monkeypatch.setattr(
        gm.httpx,
        "AsyncClient",
        _fake_client(
            {
                "access_token": "ya29.x",
                "refresh_token": "1//r",
                "expires_in": 3599,
                "scope": "https://www.googleapis.com/auth/gmail.modify",
                "token_type": "Bearer",
            }
        ),
    )
    ts = await gm.GmailOAuthProvider().exchange_code(
        code="c", redirect_uri="https://app/cb", code_verifier="v", state="s"
    )
    assert ts.access_token == "ya29.x"
    assert ts.refresh_token == "1//r"
    assert ts.expires_at_epoch > time.time()  # short-lived, set from expires_in
    assert ts.scopes == ["https://www.googleapis.com/auth/gmail.modify"]


@pytest.mark.asyncio
async def test_refresh_keeps_old_refresh_token(monkeypatch):
    # Google omits refresh_token on refresh; provider must carry the old one.
    monkeypatch.setattr(
        gm.httpx,
        "AsyncClient",
        _fake_client({"access_token": "ya29.new", "expires_in": 3599}),
    )
    ts = await gm.GmailOAuthProvider().refresh("1//old")
    assert ts.access_token == "ya29.new"
    assert ts.refresh_token == "1//old"


@pytest.mark.asyncio
async def test_refresh_failure_carries_code(monkeypatch):
    monkeypatch.setattr(
        gm.httpx, "AsyncClient", _fake_client({"error": "invalid_grant"}, status=400)
    )
    with pytest.raises(RuntimeError) as ei:
        await gm.GmailOAuthProvider().refresh("1//revoked")
    assert getattr(ei.value, "code", None) == "invalid_grant"


@pytest.mark.asyncio
async def test_fetch_external_identity(monkeypatch):
    monkeypatch.setattr(
        gm.httpx,
        "AsyncClient",
        _fake_client({"emailAddress": "me@gmail.com"}),
    )
    from server.oauth_providers import OAuthTokenSet

    ext_id, label = await gm.GmailConnector().fetch_external_identity(
        OAuthTokenSet("ya29.x", "1//r", time.time() + 3600)
    )
    assert ext_id == "me@gmail.com" and label == "me@gmail.com"
