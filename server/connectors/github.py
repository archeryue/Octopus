"""GitHub connector (connectors.md Phase B).

Descriptor + OAuth provider. The matching stdio MCP server lives in
`server/mcp_servers/connectors/github.py`. GitHub OAuth Apps use the standard
authorization-code flow with a client secret (no PKCE); classic-app tokens are
long-lived (no expiry / refresh), so the refresh path stays dead — mirroring
Notion's old behavior in the original plan.
"""

from __future__ import annotations

import time
from urllib.parse import urlencode

import httpx

from ..oauth_providers import OAuthTokenSet
from .base import ConnectorBase
from .registry import register

_API_BASE = "https://api.github.com"
_TIMEOUT = 15.0


class GitHubOAuthProvider:
    kind = "github"
    authorize_url = "https://github.com/login/oauth/authorize"
    token_url = "https://github.com/login/oauth/access_token"
    default_scopes = ["repo", "read:org"]
    pkce = False  # GitHub OAuth Apps authenticate with a client secret

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
            "scope": " ".join(self.default_scopes),
            "state": state,
            "allow_signup": "false",
        }
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
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                self.token_url,
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "code": code,
                    "redirect_uri": redirect_uri,
                },
                headers={"Accept": "application/json"},
            )
        resp.raise_for_status()
        body = resp.json()
        if "error" in body:
            raise RuntimeError(body.get("error_description") or body["error"])
        # GitHub returns scopes comma-separated; classic apps omit expires_in.
        scope = body.get("scope") or ""
        expires_in = body.get("expires_in")
        return OAuthTokenSet(
            access_token=body["access_token"],
            refresh_token=body.get("refresh_token"),
            expires_at_epoch=(time.time() + int(expires_in)) if expires_in else 0.0,
            scopes=[s for s in scope.split(",") if s],
            token_type=body.get("token_type", "bearer"),
        )

    async def refresh(
        self, *, client_id: str, client_secret: str, refresh_token: str
    ) -> OAuthTokenSet:
        # Classic OAuth-App tokens don't expire; the manager never reaches here
        # (it only refreshes when expires_at_epoch is set). GitHub Apps with
        # expiring user tokens would implement the refresh-token grant here.
        raise NotImplementedError("GitHub OAuth-App tokens do not expire")


class GitHubConnector(ConnectorBase):
    kind = "github"
    display_name = "GitHub"
    category = "dev"
    allows_multiple = True
    oauth = GitHubOAuthProvider()
    tools = (
        "search_issues",
        "get_issue",
        "get_pr",
        "list_repos",
        "get_file",
        "search_code",
        "create_issue",
        "comment",
    )
    blurb_intro = (
        "Read and act on the linked GitHub account: search issues/PRs, read "
        "repos and files, search code, open issues, and comment. Creating "
        "issues/comments writes to GitHub — say what you're about to do first."
    )
    setup_url = "https://github.com/settings/developers"
    setup_steps = (
        "Open GitHub → Settings → Developer settings → OAuth Apps → New OAuth App.",
        "Set 'Authorization callback URL' to the redirect URI above.",
        "Register the app, then click 'Generate a new client secret'.",
        "Paste the Client ID and secret below.",
    )

    async def fetch_external_identity(self, token_set: OAuthTokenSet):
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                f"{_API_BASE}/user",
                headers={
                    "Authorization": f"Bearer {token_set.access_token}",
                    "Accept": "application/vnd.github+json",
                },
            )
        resp.raise_for_status()
        data = resp.json()
        login = data["login"]
        # login can be renamed; the numeric id is the stable account key.
        return f"{login}:{data['id']}", login


register(GitHubConnector())
