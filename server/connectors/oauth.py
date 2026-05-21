"""OAuth plumbing for connectors (connectors.md §5.2, §5.5.5).

Distinct from `server/oauth_providers.py` + `server/oauth_login.py`, which are
Anthropic-flavored (they `mint_api_key` and use a code-paste callback). Connector
providers (Google, GitHub, …) use the standard redirect-URI authorization-code
flow and return an `OAuthTokenSet` directly — no API-key minting.

This module holds the provider protocol, small PKCE/state helpers, and the
in-memory pending-login manager (CSRF state + TTL). Token persistence and the
code↔token exchange are orchestrated by the router, which has the DB + registry.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol

from ..oauth_providers import OAuthTokenSet

# --- PKCE / state helpers (RFC 7636) --------------------------------------


def _b64url(raw: bytes) -> str:
    """Base64-url-encode without padding."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def gen_verifier() -> str:
    return _b64url(secrets.token_bytes(32))


def gen_state() -> str:
    return _b64url(secrets.token_bytes(24))


def challenge_from(verifier: str) -> str:
    return _b64url(hashlib.sha256(verifier.encode("ascii")).digest())


# --- provider protocol -----------------------------------------------------


class ConnectorOAuthProvider(Protocol):
    """Per-kind OAuth config. Stateless; one instance per connector kind."""

    kind: str
    authorize_url: str
    token_url: str
    default_scopes: list[str]
    pkce: bool

    def build_authorize_url(
        self, *, redirect_uri: str, code_challenge: str | None, state: str
    ) -> str:
        """The URL the user opens to consent."""
        ...

    async def exchange_code(
        self,
        *,
        code: str,
        redirect_uri: str,
        code_verifier: str | None,
        state: str,
    ) -> OAuthTokenSet:
        """POST the authorization code to the token endpoint."""
        ...

    async def refresh(self, refresh_token: str) -> OAuthTokenSet:
        """Mint a fresh access token from a refresh token."""
        ...


# --- pending-login manager (CSRF state + TTL) ------------------------------


class ConnectorLoginState(str, Enum):
    pending = "pending"
    success = "success"
    error = "error"
    cancelled = "cancelled"


@dataclass
class PendingLogin:
    login_id: str
    kind: str
    redirect_uri: str
    authorize_url: str
    state: str = ""  # raw CSRF state (the random half of the composite)
    verifier: str | None = None
    requested_label: str | None = None
    status: ConnectorLoginState = ConnectorLoginState.pending
    installation_id: str | None = None
    message: str | None = None
    created_at: float = field(default=0.0, repr=False)


_LOGIN_TTL_SECONDS = 15 * 60


class ConnectorLoginError(Exception):
    """Bad login_id or CSRF-state mismatch on the OAuth callback."""


class ConnectorLoginManager:
    """In-memory registry of in-flight OAuth installs, keyed by login_id.

    The `state` carried through the provider is `"{login_id}:{raw_state}"`, so
    the callback can find the pending login *and* verify the random half hasn't
    been tampered with (CSRF). Entries self-expire after _LOGIN_TTL_SECONDS.
    """

    def __init__(self) -> None:
        self._pending: dict[str, PendingLogin] = {}

    @staticmethod
    def _now() -> float:
        return time.monotonic()

    def start(
        self,
        *,
        provider: ConnectorOAuthProvider,
        redirect_uri: str,
        requested_label: str | None = None,
    ) -> PendingLogin:
        self._gc()
        login_id = _b64url(secrets.token_bytes(9))  # 12 url-safe chars
        raw_state = gen_state()
        verifier = gen_verifier() if provider.pkce else None
        challenge = challenge_from(verifier) if verifier else None
        authorize_url = provider.build_authorize_url(
            redirect_uri=redirect_uri,
            code_challenge=challenge,
            state=f"{login_id}:{raw_state}",
        )
        pl = PendingLogin(
            login_id=login_id,
            kind=provider.kind,
            redirect_uri=redirect_uri,
            authorize_url=authorize_url,
            state=raw_state,
            verifier=verifier,
            requested_label=requested_label,
            created_at=self._now(),
        )
        self._pending[login_id] = pl
        return pl

    def get(self, login_id: str) -> PendingLogin | None:
        return self._pending.get(login_id)

    def resolve_callback(self, composite_state: str) -> PendingLogin:
        """Validate the callback's `state` and return its pending login.

        Raises ConnectorLoginError on unknown login_id or CSRF mismatch.
        """
        login_id, _, raw_state = (composite_state or "").partition(":")
        pl = self._pending.get(login_id)
        if pl is None:
            raise ConnectorLoginError("unknown or expired login")
        if not raw_state or not secrets.compare_digest(raw_state, pl.state):
            raise ConnectorLoginError("state mismatch (possible CSRF)")
        return pl

    def mark_success(self, login_id: str, installation_id: str) -> None:
        pl = self._pending.get(login_id)
        if pl is not None:
            pl.status = ConnectorLoginState.success
            pl.installation_id = installation_id

    def mark_error(self, login_id: str, message: str) -> None:
        pl = self._pending.get(login_id)
        if pl is not None:
            pl.status = ConnectorLoginState.error
            pl.message = message

    def cancel(self, login_id: str) -> bool:
        pl = self._pending.get(login_id)
        if pl is None:
            return False
        pl.status = ConnectorLoginState.cancelled
        return True

    def _gc(self) -> None:
        now = self._now()
        stale = [
            lid
            for lid, pl in self._pending.items()
            if now - pl.created_at > _LOGIN_TTL_SECONDS
        ]
        for lid in stale:
            self._pending.pop(lid, None)
