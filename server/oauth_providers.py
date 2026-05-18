"""OAuth provider registry — pluggable backends for the login flow.

Each provider knows how to:
  - build an authorize URL (with PKCE challenge + state)
  - exchange the user-pasted code for a short-lived access token
  - mint a long-lived API key from that access token

The `OAuthLoginManager` (in `oauth_login.py`) is provider-agnostic — it
just calls `PROVIDERS[name].build_authorize_url(...)`,
`.exchange_code(...)`, `.mint_api_key(...)`. Adding GitHub / Lark / Codex
later is a new class in this module + a `PROVIDERS[...] = ...` entry.

For now only Claude Code is wired (its constants were reverse-engineered
from the bundled CLI v2.1.143 — see `docs/cli-protocol-notes.md`).
"""

from __future__ import annotations

from typing import Protocol
from urllib.parse import urlencode

import httpx


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
    ) -> str:
        """POST the auth code to the token endpoint. Returns access_token."""
        ...

    async def mint_api_key(self, access_token: str) -> str:
        """Trade the access_token for a long-lived API key the agent can use."""
        ...


class ClaudeCodeProvider:
    """Claude.ai OAuth → `sk-ant-…` long-lived key.

    Constants taken from the bundled `claude` CLI (v2.1.143). The flow:

      1. authorize URL on claude.ai (manual redirect to a callback page
         that shows the code + state for the user to copy back)
      2. POST code + PKCE verifier to console.anthropic.com → access_token
      3. POST access_token to api.anthropic.com → sk-ant- key

    Steps 2 and 3 are sequential; if either upstream fails the exception
    bubbles up with a snippet of the response body.
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
    ) -> str:
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
        body = resp.json()
        access_token = body.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise RuntimeError(
                f"token response missing access_token; keys={list(body.keys())}"
            )
        return access_token

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
