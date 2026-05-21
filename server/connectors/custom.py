"""Custom (user-defined) connectors (connectors.md custom-connectors).

A custom connector is defined entirely from the browser — its OAuth endpoints,
scopes, and API base — and stored in the DB (no server code). `GenericOAuth
Provider` drives the standard OAuth2 flow from those fields, and a single
generic MCP server (`server.mcp_servers.connectors.custom`) exposes one
authenticated `request` tool scoped to the connector's API base.
"""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import urlencode

import httpx

from ..oauth_providers import OAuthTokenSet
from .base import ConnectorBase
from .registry import get_connector as _get_builtin

_TIMEOUT = 15.0


class GenericOAuthProvider:
    """Standard OAuth2 provider built from a custom-connector definition.

    Handles the common shapes: form-encoded token request, JSON response
    (Accept: application/json), space- or comma-separated scopes, optional
    PKCE, and the refresh-token grant.
    """

    def __init__(
        self,
        *,
        kind: str,
        authorize_url: str,
        token_url: str,
        default_scopes: list[str],
        pkce: bool,
    ) -> None:
        self.kind = kind
        self.authorize_url = authorize_url
        self.token_url = token_url
        self.default_scopes = list(default_scopes)
        self.pkce = pkce

    def build_authorize_url(
        self, *, client_id: str, redirect_uri: str, code_challenge: str | None, state: str
    ) -> str:
        params: dict[str, str] = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "state": state,
        }
        if self.default_scopes:
            params["scope"] = " ".join(self.default_scopes)
        if self.pkce and code_challenge:
            params["code_challenge"] = code_challenge
            params["code_challenge_method"] = "S256"
        sep = "&" if "?" in self.authorize_url else "?"
        return f"{self.authorize_url}{sep}{urlencode(params)}"

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
            resp = await client.post(
                self.token_url, data=data, headers={"Accept": "application/json"}
            )
        resp.raise_for_status()
        body = resp.json()
        if "error" in body:
            raise RuntimeError(body.get("error_description") or body["error"])
        return self._parse(body)

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
                headers={"Accept": "application/json"},
            )
        if resp.status_code != 200:
            body = resp.json() if resp.content else {}
            err = RuntimeError(body.get("error") or f"HTTP {resp.status_code}")
            err.code = body.get("error", "refresh_failed")  # type: ignore[attr-defined]
            raise err
        ts = self._parse(resp.json())
        if ts.refresh_token is None:
            ts.refresh_token = refresh_token
        return ts

    @staticmethod
    def _parse(body: dict) -> OAuthTokenSet:
        scope = body.get("scope") or ""
        scopes = scope.replace(",", " ").split()
        expires_in = body.get("expires_in")
        return OAuthTokenSet(
            access_token=body["access_token"],
            refresh_token=body.get("refresh_token"),
            expires_at_epoch=(time.time() + int(expires_in)) if expires_in else 0.0,
            scopes=scopes,
            token_type=body.get("token_type", "Bearer"),
        )


class CustomConnector(ConnectorBase):
    """A connector backed by a DB definition rather than code. Exposes one
    generic `request` tool scoped to its API base."""

    category = "custom"
    allows_multiple = True
    tools = ("request",)
    is_custom = True

    def __init__(self, row: dict[str, Any]) -> None:
        self.kind = row["kind"]
        self.display_name = row["display_name"]
        self.api_base = str(row["api_base"]).rstrip("/")
        self.oauth = GenericOAuthProvider(
            kind=row["kind"],
            authorize_url=row["authorize_url"],
            token_url=row["token_url"],
            default_scopes=row.get("scopes") or [],
            pkce=bool(row.get("pkce")),
        )
        self.blurb_intro = (
            f"Authenticated HTTP access to {self.display_name} at "
            f"{self.api_base}. Call request(method, path, query?, body?) — path "
            "is relative to that base."
        )

    @property
    def mcp_module(self) -> str:
        return "server.mcp_servers.connectors.custom"

    def mcp_entry(self, installation, callback_env):
        entry = super().mcp_entry(installation, callback_env)
        # The generic server needs the API base to call.
        entry["env"]["OCTOPUS_CONNECTOR_API_BASE"] = self.api_base
        return entry

    async def fetch_external_identity(self, token_set):
        # No known profile endpoint for an arbitrary API; label by name.
        return "", self.display_name


async def resolve_connector(db, kind: str) -> ConnectorBase | None:
    """A connector by kind: a built-in (code) connector, else a CustomConnector
    built from its DB definition, else None."""
    builtin = _get_builtin(kind)
    if builtin is not None:
        return builtin
    row = await db.get_custom_connector(kind)
    return CustomConnector(row) if row else None
