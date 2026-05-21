"""Gmail connector (connectors.md Phase C / §6.2).

Descriptor + OAuth provider. Unlike GitHub, Google access tokens expire
(~1h) and carry a long-lived refresh token, so `refresh` is a live path that
ConnectorManager calls on near-expiry. PKCE + access_type=offline +
prompt=consent are required to reliably receive a refresh token.
"""

from __future__ import annotations

import time
from urllib.parse import urlencode

import httpx

from ..oauth_providers import OAuthTokenSet
from .base import ConnectorBase
from .registry import register

_GMAIL_API = "https://gmail.googleapis.com/gmail/v1"
_TIMEOUT = 15.0


class GmailOAuthProvider:
    kind = "gmail"
    authorize_url = "https://accounts.google.com/o/oauth2/v2/auth"
    token_url = "https://oauth2.googleapis.com/token"
    # gmail.modify covers read, label, draft and send (explicit user consent
    # is shown in the Google browser screen).
    default_scopes = ["https://www.googleapis.com/auth/gmail.modify"]
    pkce = True

    def build_authorize_url(
        self,
        *,
        client_id: str,
        redirect_uri: str,
        code_challenge: str | None,
        state: str,
    ) -> str:
        params = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(self.default_scopes),
            "state": state,
            # offline + consent are what actually yield a refresh token.
            "access_type": "offline",
            "prompt": "consent",
        }
        if code_challenge:
            params["code_challenge"] = code_challenge
            params["code_challenge_method"] = "S256"
        return f"{self.authorize_url}?{urlencode(params)}"

    async def exchange_code(
        self,
        *,
        client_id: str,
        client_secret: str,
        code: str,
        redirect_uri: str,
        code_verifier: str | None,
        state: str,
    ) -> OAuthTokenSet:
        data = {
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        }
        if code_verifier:
            data["code_verifier"] = code_verifier
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(self.token_url, data=data)
        resp.raise_for_status()
        return self._parse(resp.json())

    async def refresh(
        self, *, client_id: str, client_secret: str, refresh_token: str
    ) -> OAuthTokenSet:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                self.token_url,
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token",
                },
            )
        if resp.status_code != 200:
            # invalid_grant = user revoked access; surface a stable code so the
            # manager flips needs_reconnect with a meaningful reason.
            body = resp.json() if resp.content else {}
            err = RuntimeError(body.get("error") or f"HTTP {resp.status_code}")
            err.code = body.get("error", "refresh_failed")  # type: ignore[attr-defined]
            raise err
        ts = self._parse(resp.json())
        # Google omits refresh_token on refresh responses; keep the old one.
        if ts.refresh_token is None:
            ts.refresh_token = refresh_token
        return ts

    @staticmethod
    def _parse(body: dict) -> OAuthTokenSet:
        expires_in = body.get("expires_in")
        scope = body.get("scope") or ""
        return OAuthTokenSet(
            access_token=body["access_token"],
            refresh_token=body.get("refresh_token"),
            expires_at_epoch=(time.time() + int(expires_in)) if expires_in else 0.0,
            scopes=scope.split(),
            token_type=body.get("token_type", "Bearer"),
        )


class GmailConnector(ConnectorBase):
    kind = "gmail"
    display_name = "Gmail"
    category = "email"
    allows_multiple = True
    oauth = GmailOAuthProvider()
    tools = (
        "search",
        "get",
        "list_labels",
        "create_draft",
        "send_draft",
        "label",
        "unlabel",
    )
    blurb_intro = (
        "Read and triage the linked Gmail account: search, read messages, "
        "manage labels, and draft replies. Composing a draft is safe. "
        "Before calling send_draft, ALWAYS show the drafted message and get "
        "an explicit yes via mcp__ask__user in the same turn — never send "
        "without the user's OK."
    )
    setup_url = "https://console.cloud.google.com/apis/credentials"
    setup_steps = (
        "Enable the Gmail API for your project "
        "(console.cloud.google.com/apis/library/gmail.googleapis.com) — without "
        "this, sign-in succeeds but the profile lookup fails with 403.",
        "Google Auth Platform → Audience (console.cloud.google.com/auth/audience) "
        "→ Test users → Add users → your own Gmail address. REQUIRED while the "
        "app is in Testing, or sign-in is blocked with a 403.",
        "Credentials → Create credentials → OAuth client ID → Web application.",
        "Add the redirect URI above under 'Authorized redirect URIs'.",
        "Paste the Client ID and secret below.",
        "Note: while the app stays in 'Testing', Google expires the refresh "
        "token after ~7 days, so you'll re-connect periodically.",
    )

    async def fetch_external_identity(self, token_set: OAuthTokenSet):
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                f"{_GMAIL_API}/users/me/profile",
                headers={"Authorization": f"Bearer {token_set.access_token}"},
            )
        resp.raise_for_status()
        email = resp.json()["emailAddress"]
        return email, email


register(GmailConnector())
