"""In-app OAuth login orchestrator — provider-agnostic.

Drives the PKCE+manual-paste flow:

  1. `start(provider_name)` generates a PKCE pair + state, asks the
     provider for an authorize URL, returns both to the caller.
  2. User opens URL, signs in, copies the `<code>#<state>` blob shown
     on the provider's callback page.
  3. `submit_code(login_id, raw_code)` splits + validates state, calls
     the provider's token exchange, then its api-key mint.

Per-provider knowledge (endpoints, client id, scopes, payload shapes)
lives in `server/oauth_providers.py`. This module owns lifecycle:
in-flight session bookkeeping, PKCE, state validation, error surfacing.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import secrets
import uuid
from dataclasses import dataclass, field
from enum import Enum

from .oauth_errors import ScopeMissingError
from .oauth_providers import OAuthProvider, OAuthTokenSet, get_provider

logger = logging.getLogger(__name__)

# Default provider — used when callers don't specify one (back-compat for
# the pre-multi-provider API where everything was Claude).
DEFAULT_PROVIDER = "claude-code"

# Login attempts that never complete shouldn't accumulate forever.
_LOGIN_TTL_SECONDS = 15 * 60


class LoginState(str, Enum):
    awaiting_code = "awaiting_code"
    finalizing = "finalizing"
    success = "success"
    error = "error"
    cancelled = "cancelled"


@dataclass
class LoginSession:
    id: str
    url: str
    provider_name: str = DEFAULT_PROVIDER
    state: LoginState = LoginState.awaiting_code
    # Populated when the user's account can mint a long-lived API key
    # (Console org with `org:create_api_key` scope). Mutually exclusive
    # with `oauth_tokens`.
    token: str | None = None
    # Populated when the user's account can't mint an API key (Pro/Max
    # subscriber without an org). The OAuthTokenSet is what gets persisted
    # as the credential's secret in that case.
    oauth_tokens: OAuthTokenSet | None = None
    message: str | None = None
    _verifier: str = field(default="", repr=False)
    _state: str = field(default="", repr=False)
    _created_at: float = field(default=0.0, repr=False)


class OAuthLoginManager:
    """Holds in-flight OAuth attempts. Single instance, app-lifetime."""

    def __init__(self) -> None:
        self._sessions: dict[str, LoginSession] = {}

    # ---------------------------------------------------------------- public

    async def start(self, provider_name: str = DEFAULT_PROVIDER) -> LoginSession:
        """Begin an OAuth login for the named provider.

        Returns the LoginSession with `state == awaiting_code` and `url`
        populated. Raises KeyError if the provider isn't registered.
        """
        provider = get_provider(provider_name)
        verifier = _gen_verifier()
        state = _gen_state()
        challenge = _challenge_from(verifier)

        url = provider.build_authorize_url(code_challenge=challenge, state=state)

        login_id = uuid.uuid4().hex[:16]
        loop = asyncio.get_running_loop()
        session = LoginSession(
            id=login_id,
            url=url,
            provider_name=provider_name,
            _verifier=verifier,
            _state=state,
            _created_at=loop.time(),
        )
        self._sessions[login_id] = session
        self._gc()

        logger.info(
            "OAuth login %s: started (%s), url=%s", login_id, provider_name, url
        )
        return session

    async def submit_code(self, login_id: str, raw_code: str) -> LoginSession:
        """Exchange the user-pasted code for a long-lived API key."""
        session = self._sessions.get(login_id)
        if session is None:
            raise KeyError(f"unknown login id: {login_id}")
        if session.state != LoginState.awaiting_code:
            raise RuntimeError(
                f"login {login_id} is in state {session.state}, cannot accept code"
            )

        provider = get_provider(session.provider_name)
        code, code_state = _split_code(raw_code)
        if code_state and code_state != session._state:
            session.state = LoginState.error
            session.message = "state mismatch — possible CSRF; restart the login"
            logger.warning("OAuth login %s: %s", login_id, session.message)
            raise RuntimeError(session.message)

        session.state = LoginState.finalizing
        logger.info("OAuth login %s: exchanging code for access token", login_id)
        try:
            token_set = await provider.exchange_code(
                code=code, code_verifier=session._verifier, state=session._state
            )
        except Exception as e:
            session.state = LoginState.error
            session.message = f"token exchange failed: {e}"
            logger.warning("OAuth login %s: %s", login_id, session.message)
            raise RuntimeError(session.message) from e

        logger.info(
            "OAuth login %s: minting long-lived API key", login_id
        )
        try:
            api_key = await provider.mint_api_key(token_set.access_token)
        except ScopeMissingError as e:
            # Personal-account / Pro / Max user — no org, so the OAuth
            # token wasn't granted org:create_api_key. Keep the token set
            # itself; the credential will use CLAUDE_CODE_OAUTH_TOKEN with
            # periodic refresh instead of a long-lived sk-ant- key.
            if token_set.refresh_token is None:
                session.state = LoginState.error
                session.message = (
                    "API key creation failed and the OAuth response didn't "
                    "include a refresh token — can't fall back to OAuth auth"
                )
                logger.warning("OAuth login %s: %s", login_id, session.message)
                raise RuntimeError(session.message) from e
            session.oauth_tokens = token_set
            session.state = LoginState.success
            logger.info(
                "OAuth login %s: missing %s scope — storing OAuth token set instead",
                login_id,
                e.missing_scope,
            )
            return session
        except Exception as e:
            session.state = LoginState.error
            session.message = f"API key creation failed: {e}"
            logger.warning("OAuth login %s: %s", login_id, session.message)
            raise RuntimeError(session.message) from e

        session.token = api_key
        session.state = LoginState.success
        logger.info("OAuth login %s: success", login_id)
        return session

    async def cancel(self, login_id: str) -> None:
        session = self._sessions.get(login_id)
        if session is None:
            return
        if session.state in (LoginState.success, LoginState.error, LoginState.cancelled):
            return
        session.state = LoginState.cancelled
        session.message = "cancelled by user"
        logger.info("OAuth login %s: cancelled", login_id)

    def get(self, login_id: str) -> LoginSession | None:
        return self._sessions.get(login_id)

    async def shutdown(self) -> None:
        """Nothing to tear down (no subprocesses) — just drop state."""
        self._sessions.clear()

    # ---------------------------------------------------------------- internals

    def _gc(self) -> None:
        """Drop login sessions older than _LOGIN_TTL_SECONDS."""
        loop = asyncio.get_running_loop()
        now = loop.time()
        stale = [
            sid
            for sid, s in self._sessions.items()
            if now - s._created_at > _LOGIN_TTL_SECONDS
        ]
        for sid in stale:
            self._sessions.pop(sid, None)


# --------------------------------------------------------------------------- helpers


def _b64url(raw: bytes) -> str:
    """Base64-url-encode without padding (RFC 7636 §4.1)."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _gen_verifier() -> str:
    return _b64url(secrets.token_bytes(32))


def _gen_state() -> str:
    return _b64url(secrets.token_bytes(32))


def _challenge_from(verifier: str) -> str:
    return _b64url(hashlib.sha256(verifier.encode("ascii")).digest())


def _split_code(raw: str) -> tuple[str, str | None]:
    """Parse the OAuth code the user pasted.

    The Anthropic callback page formats it as `<code>#<state>`. We accept
    either form so users who only pasted the code half still work.
    """
    raw = raw.strip()
    if "#" in raw:
        code, _, state = raw.partition("#")
        return code.strip(), state.strip() or None
    return raw, None


# Singleton — wired into the FastAPI lifespan in main.py.
oauth_login_manager = OAuthLoginManager()


# --------------------------------------------------------------------------- back-compat


# The Claude-specific constants used to live at module scope; keeping
# re-exports so existing tests (and any external callers) don't break.
# New code should reach into `server.oauth_providers` instead.
def _claude() -> OAuthProvider:
    return get_provider("claude-code")


CLIENT_ID = _claude().CLIENT_ID  # type: ignore[attr-defined]
AUTHORIZE_URL = _claude().AUTHORIZE_URL  # type: ignore[attr-defined]
TOKEN_URL = _claude().TOKEN_URL  # type: ignore[attr-defined]
API_KEY_URL = _claude().API_KEY_URL  # type: ignore[attr-defined]
MANUAL_REDIRECT_URI = _claude().MANUAL_REDIRECT_URI  # type: ignore[attr-defined]
SCOPES = list(_claude().SCOPES)  # type: ignore[attr-defined]
