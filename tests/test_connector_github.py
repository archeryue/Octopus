"""GitHub connector descriptor + OAuth provider (connectors.md Phase B / §6).
Client creds are passed explicitly (resolved from DB/env by the manager). HTTP
is mocked by monkeypatching the module's httpx.AsyncClient."""

from __future__ import annotations

import pytest

from server.connectors import github as gh
from server.connectors.registry import get_connector


def _fake_client(body: dict, status: int = 200, capture: dict | None = None):
    class FakeResponse:
        status_code = status

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
                capture["url"], capture["data"], capture["headers"] = url, data, headers
            return FakeResponse()

        async def get(self, url, headers=None):
            if capture is not None:
                capture["url"], capture["headers"] = url, headers
            return FakeResponse()

    return FakeClient


def test_github_is_registered():
    assert isinstance(get_connector("github"), gh.GitHubConnector)


def test_build_authorize_url():
    url = gh.GitHubOAuthProvider().build_authorize_url(
        client_id="cid", redirect_uri="https://app/cb", code_challenge=None, state="lid:rnd"
    )
    assert url.startswith("https://github.com/login/oauth/authorize?")
    assert "client_id=cid" in url
    assert "state=lid%3Arnd" in url
    assert "scope=repo+read%3Aorg" in url


@pytest.mark.asyncio
async def test_exchange_code_parses_token(monkeypatch):
    cap: dict = {}
    monkeypatch.setattr(
        gh.httpx,
        "AsyncClient",
        _fake_client(
            {"access_token": "gho_x", "scope": "repo,read:org", "token_type": "bearer"},
            capture=cap,
        ),
    )
    ts = await gh.GitHubOAuthProvider().exchange_code(
        client_id="cid",
        client_secret="csec",
        code="c",
        redirect_uri="https://app/cb",
        code_verifier=None,
        state="s",
    )
    assert ts.access_token == "gho_x"
    assert ts.scopes == ["repo", "read:org"]
    assert ts.expires_at_epoch == 0.0  # classic app token: non-expiring
    assert ts.refresh_token is None
    # The passed-in client secret reaches GitHub's token endpoint.
    assert cap["data"]["client_id"] == "cid"
    assert cap["data"]["client_secret"] == "csec"


@pytest.mark.asyncio
async def test_exchange_code_surfaces_error(monkeypatch):
    monkeypatch.setattr(
        gh.httpx,
        "AsyncClient",
        _fake_client(
            {"error": "bad_verification_code", "error_description": "expired"}
        ),
    )
    with pytest.raises(RuntimeError, match="expired"):
        await gh.GitHubOAuthProvider().exchange_code(
            client_id="cid",
            client_secret="csec",
            code="c",
            redirect_uri="https://app/cb",
            code_verifier=None,
            state="s",
        )


@pytest.mark.asyncio
async def test_fetch_external_identity(monkeypatch):
    cap: dict = {}
    monkeypatch.setattr(
        gh.httpx,
        "AsyncClient",
        _fake_client({"login": "octocat", "id": 583231}, capture=cap),
    )
    from server.oauth_providers import OAuthTokenSet

    ext_id, label = await gh.GitHubConnector().fetch_external_identity(
        OAuthTokenSet("gho_x", None, 0.0)
    )
    assert ext_id == "octocat:583231"  # stable numeric id, not just login
    assert label == "octocat"
    assert cap["headers"]["Authorization"] == "Bearer gho_x"
