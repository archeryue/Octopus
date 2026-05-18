"""OAuth provider registry — pluggable backends for the login flow.

Each provider knows how to:
  - build an authorize URL (with PKCE challenge + state)
  - exchange the user-pasted code for an `OAuthTokenSet`
    (access + refresh + expires_at)
  - optionally mint a long-lived API key from that access token
  - refresh an expired access token using its refresh token

The `OAuthLoginManager` (in `oauth_login.py`) is provider-agnostic — it
just calls `PROVIDERS[name].build_authorize_url(...)`,
`.exchange_code(...)`, `.mint_api_key(...)`, `.refresh_access_token(...)`.
Adding GitHub / Lark / Codex later is a new class in this module + a
`PROVIDERS[...] = ...` entry.

For Claude Code: `mint_api_key` requires the OAuth token to have the
`org:create_api_key` scope. Pro/Max subscribers without an org never
get that scope; for them the login manager falls back to keeping the
OAuthTokenSet directly. See `oauth_errors.ScopeMissingError`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urlencode

import httpx

from .oauth_errors import ScopeMissingError


@dataclass
class OAuthTokenSet:
    """The product of an OAuth code exchange or refresh.

    All Anthropic OAuth flows return access + refresh + expires_in. We
    store `expires_at_epoch` (absolute) rather than a relative duration
    so refresh logic doesn't need to remember when the call returned.

    `scopes` is the granted scope list (the server may grant fewer than
    requested — Pro/Max accounts drop `org:create_api_key`).
    """

    access_token: str
    refresh_token: str | None
    expires_at_epoch: float
    scopes: list[str] = field(default_factory=list)
    token_type: str = "Bearer"


class OAuthProvider(Protocol):
    """The contract every provider must satisfy.

    Stateless: providers hold only the constants for the upstream OAuth
    endpoints; per-login state (PKCE verifier, state token) lives on the
    `OAuthLoginManager`'s `LoginSession`.
    """

    name: str

    def build_authorize_url(self, *, code_challenge: str, state: str) -> str:
        """Build the URL the user opens in their browser to consent."""
        ...

    async def exchange_code(
        self, *, code: str, code_verifier: str, state: str
    ) -> OAuthTokenSet:
        """POST the auth code to the token endpoint. Returns the full token set."""
        ...

    async def mint_api_key(self, access_token: str) -> str:
        """Trade the access_token for a long-lived API key the agent can use.

        Raises `ScopeMissingError` when the token lacks `org:create_api_key`
        (the user is a Pro/Max subscriber without a Console org). The login
        manager treats that as the signal to keep the OAuthTokenSet itself.
        """
        ...

    async def refresh_access_token(self, refresh_token: str) -> OAuthTokenSet:
        """Mint a fresh access_token from a refresh_token."""
        ...


class ClaudeCodeProvider:
    """Claude.ai OAuth.

    Constants taken from the bundled `claude` CLI (v2.1.143). Two
    completion shapes depending on the user's account:

      Console org users (have `org:create_api_key` scope):
        1. authorize URL on claude.ai → manual code paste
        2. POST code + PKCE verifier to console.anthropic.com → OAuthTokenSet
        3. POST access_token to api.anthropic.com → sk-ant-api03- key
           (long-lived, no refresh ever)

      Pro/Max subscribers without a Console org (no `org:create_api_key`):
        Steps 1 + 2 succeed; step 3 returns 403, which we raise as
        `ScopeMissingError`. The orchestrator stores the OAuthTokenSet
        and uses `CLAUDE_CODE_OAUTH_TOKEN` env var at runtime, refreshing
        the access_token on demand via `refresh_access_token`.
    """

    name = "claude-code"

    CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
    AUTHORIZE_URL = "https://claude.ai/oauth/authorize"
    TOKEN_URL = "https://console.anthropic.com/v1/oauth/token"
    API_KEY_URL = "https://api.anthropic.com/api/oauth/claude_cli/create_api_key"
    MANUAL_REDIRECT_URI = "https://console.anthropic.com/oauth/code/callback"
    SCOPES = ["org:create_api_key", "user:profile", "user:inference"]

    TOKEN_EXCHANGE_TIMEOUT = 20.0
    API_KEY_TIMEOUT = 20.0
    REFRESH_TIMEOUT = 20.0

    # Scope required for `mint_api_key`. If the token endpoint's response
    # scope list omits this, we won't even bother calling the API-key
    # endpoint — we know it'll 403.
    _API_KEY_SCOPE = "org:create_api_key"

    def build_authorize_url(self, *, code_challenge: str, state: str) -> str:
        params = {
            "client_id": self.CLIENT_ID,
            "response_type": "code",
            "redirect_uri": self.MANUAL_REDIRECT_URI,
            "scope": " ".join(self.SCOPES),
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "state": state,
        }
        return f"{self.AUTHORIZE_URL}?{urlencode(params)}"

    async def exchange_code(
        self, *, code: str, code_verifier: str, state: str
    ) -> OAuthTokenSet:
        payload = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.MANUAL_REDIRECT_URI,
            "client_id": self.CLIENT_ID,
            "code_verifier": code_verifier,
            "state": state,
        }
        async with httpx.AsyncClient(timeout=self.TOKEN_EXCHANGE_TIMEOUT) as client:
            resp = await client.post(self.TOKEN_URL, json=payload)
        if resp.status_code != 200:
            raise RuntimeError(
                f"token endpoint returned {resp.status_code}: {resp.text[:300]}"
            )
        return self._parse_token_response(resp.json())

    async def mint_api_key(self, access_token: str) -> str:
        async with httpx.AsyncClient(timeout=self.API_KEY_TIMEOUT) as client:
            resp = await client.post(
                self.API_KEY_URL,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                },
                json={},
            )
        if resp.status_code == 403 and self._API_KEY_SCOPE in resp.text:
            # Surfaced as scope error so the caller can route the user into
            # the OAuth-token-storage path instead.
            raise ScopeMissingError(
                f"api-key endpoint returned 403 — token lacks "
                f"{self._API_KEY_SCOPE} scope: {resp.text[:300]}",
                missing_scope=self._API_KEY_SCOPE,
            )
        if resp.status_code not in (200, 201):
            raise RuntimeError(
                f"api-key endpoint returned {resp.status_code}: {resp.text[:300]}"
            )
        body = resp.json()
        # Be defensive about the exact field name in case Anthropic renames it.
        for key in ("raw_key", "api_key", "key", "value"):
            v = body.get(key)
            if isinstance(v, str) and v.startswith("sk-ant-"):
                return v
        raise RuntimeError(
            f"api-key response didn't include a sk-ant- key; keys={list(body.keys())}"
        )

    async def refresh_access_token(self, refresh_token: str) -> OAuthTokenSet:
        payload = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": self.CLIENT_ID,
        }
        async with httpx.AsyncClient(timeout=self.REFRESH_TIMEOUT) as client:
            resp = await client.post(self.TOKEN_URL, json=payload)
        if resp.status_code != 200:
            raise RuntimeError(
                f"refresh endpoint returned {resp.status_code}: {resp.text[:300]}"
            )
        body = resp.json()
        # Some OAuth servers omit `refresh_token` on a refresh response when
        # the existing one stays valid. Keep the old one in that case.
        ts = self._parse_token_response(body)
        if ts.refresh_token is None:
            ts.refresh_token = refresh_token
        return ts

    def _parse_token_response(self, body: dict) -> OAuthTokenSet:
        access_token = body.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise RuntimeError(
                f"token response missing access_token; keys={list(body.keys())}"
            )
        expires_in = body.get("expires_in")
        # Default to a conservative 1 hour if the server omits expires_in
        # (refresh logic will then kick in earlier — safer than assuming
        # indefinite life).
        if not isinstance(expires_in, (int, float)) or expires_in <= 0:
            expires_in = 3600
        scope_raw = body.get("scope") or ""
        scopes = scope_raw.split() if isinstance(scope_raw, str) else []
        return OAuthTokenSet(
            access_token=access_token,
            refresh_token=body.get("refresh_token") or None,
            expires_at_epoch=time.time() + float(expires_in),
            scopes=scopes,
            token_type=body.get("token_type", "Bearer"),
        )


# Registry. Add new providers by registering them here.
PROVIDERS: dict[str, OAuthProvider] = {
    ClaudeCodeProvider.name: ClaudeCodeProvider(),
}


def get_provider(name: str) -> OAuthProvider:
    """Look up a provider by `name`. Raises KeyError if unregistered."""
    provider = PROVIDERS.get(name)
    if provider is None:
        raise KeyError(
            f"unknown OAuth provider: {name!r}. "
            f"Registered: {sorted(PROVIDERS.keys())}"
        )
    return provider
