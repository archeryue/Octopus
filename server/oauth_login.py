"""In-app OAuth login for Claude Code subscriptions — pure-Python PKCE flow.

We do the OAuth ourselves over HTTP rather than driving `claude setup-token`
in a subprocess. The constants below come from the bundled Claude Code CLI
(reverse-engineered from cli.js v2.1.143). Flow shape mirrors what the CLI
itself does in manual-redirect mode, just in Python:

  1. Server generates a PKCE code_verifier + state.
  2. Server builds the claude.ai authorize URL, hands it to the WebUI.
  3. User opens the URL, logs in, gets redirected to
     console.anthropic.com/oauth/code/callback which displays a code in
     the form "<code>#<state>".
  4. User pastes that string back to Octopus.
  5. Server splits on '#', verifies state, POSTs the code to the token
     endpoint to get an access_token, then POSTs that to the api-key
     endpoint to get a long-lived `sk-ant-…` key.
  6. Key is stored as a normal credential (encrypted), and sessions
     use it via the existing ANTHROPIC_API_KEY env injection.
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
from typing import Any
from urllib.parse import urlencode

import httpx

logger = logging.getLogger(__name__)

# -- OAuth provider constants (from Claude Code CLI v2.1.143) ----------------

CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
AUTHORIZE_URL = "https://claude.ai/oauth/authorize"
TOKEN_URL = "https://console.anthropic.com/v1/oauth/token"
API_KEY_URL = "https://api.anthropic.com/api/oauth/claude_cli/create_api_key"
MANUAL_REDIRECT_URI = "https://console.anthropic.com/oauth/code/callback"
SCOPES = ["org:create_api_key", "user:profile", "user:inference"]

# Login attempts that never complete shouldn't accumulate forever.
_LOGIN_TTL_SECONDS = 15 * 60

# Network timeouts.
_TOKEN_EXCHANGE_TIMEOUT = 20.0
_API_KEY_TIMEOUT = 20.0


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
    state: LoginState = LoginState.awaiting_code
    token: str | None = None
    message: str | None = None
    _verifier: str = field(default="", repr=False)
    _state: str = field(default="", repr=False)
    _created_at: float = field(default=0.0, repr=False)


class OAuthLoginManager:
    """Holds in-flight OAuth attempts. Single instance, app-lifetime."""

    def __init__(self) -> None:
        self._sessions: dict[str, LoginSession] = {}

    # ---------------------------------------------------------------- public

    async def start(self) -> LoginSession:
        """Generate a PKCE pair, build the authorize URL, return both."""
        verifier = _gen_verifier()
        state = _gen_state()
        challenge = _challenge_from(verifier)

        params = {
            "client_id": CLIENT_ID,
            "response_type": "code",
            "redirect_uri": MANUAL_REDIRECT_URI,
            "scope": " ".join(SCOPES),
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
        }
        url = f"{AUTHORIZE_URL}?{urlencode(params)}"

        login_id = uuid.uuid4().hex[:16]
        loop = asyncio.get_running_loop()
        session = LoginSession(
            id=login_id,
            url=url,
            _verifier=verifier,
            _state=state,
            _created_at=loop.time(),
        )
        self._sessions[login_id] = session
        self._gc()

        logger.info("OAuth login %s: started, url=%s", login_id, url)
        return session

    async def submit_code(self, login_id: str, raw_code: str) -> LoginSession:
        """Exchange the user-pasted code for a long-lived API key.

        `raw_code` is what the user copied from the OAuth callback page,
        which is in the form `<authorization_code>#<state>`.
        """
        session = self._sessions.get(login_id)
        if session is None:
            raise KeyError(f"unknown login id: {login_id}")
        if session.state != LoginState.awaiting_code:
            raise RuntimeError(
                f"login {login_id} is in state {session.state}, cannot accept code"
            )

        code, state = _split_code(raw_code)
        if state and state != session._state:
            session.state = LoginState.error
            session.message = "state mismatch — possible CSRF; restart the login"
            logger.warning("OAuth login %s: %s", login_id, session.message)
            raise RuntimeError(session.message)

        session.state = LoginState.finalizing
        logger.info("OAuth login %s: exchanging code for token", login_id)

        try:
            access_token = await _exchange_code_for_access_token(
                code=code,
                code_verifier=session._verifier,
                state=session._state,
            )
        except Exception as e:
            session.state = LoginState.error
            session.message = f"token exchange failed: {e}"
            logger.warning("OAuth login %s: %s", login_id, session.message)
            raise RuntimeError(session.message) from e

        logger.info(
            "OAuth login %s: token exchange ok, creating long-lived API key",
            login_id,
        )
        try:
            api_key = await _create_long_lived_api_key(access_token)
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
        """No subprocesses to tear down anymore — just drop state."""
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
    # CLI uses 32 random bytes; spec allows 43-128 chars after b64url.
    return _b64url(secrets.token_bytes(32))


def _gen_state() -> str:
    return _b64url(secrets.token_bytes(32))


def _challenge_from(verifier: str) -> str:
    return _b64url(hashlib.sha256(verifier.encode("ascii")).digest())


def _split_code(raw: str) -> tuple[str, str | None]:
    """Parse the OAuth code the user pasted.

    Anthropic's callback page formats it as `<code>#<state>`. We accept
    either form so users who only pasted the code half still work.
    """
    raw = raw.strip()
    if "#" in raw:
        code, _, state = raw.partition("#")
        return code.strip(), state.strip() or None
    return raw, None


async def _exchange_code_for_access_token(
    *, code: str, code_verifier: str, state: str
) -> str:
    """POST the authorization code to the token endpoint."""
    payload = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": MANUAL_REDIRECT_URI,
        "client_id": CLIENT_ID,
        "code_verifier": code_verifier,
        "state": state,
    }
    async with httpx.AsyncClient(timeout=_TOKEN_EXCHANGE_TIMEOUT) as client:
        resp = await client.post(TOKEN_URL, json=payload)
    if resp.status_code != 200:
        snippet = resp.text[:300]
        raise RuntimeError(
            f"token endpoint returned {resp.status_code}: {snippet}"
        )
    body = resp.json()
    access_token = body.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise RuntimeError(
            f"token response missing access_token; keys={list(body.keys())}"
        )
    return access_token


async def _create_long_lived_api_key(access_token: str) -> str:
    """Trade the OAuth access_token for a long-lived `sk-ant-…` API key.

    Mirrors what `claude /login` does after a successful OAuth: the access
    token is short-lived; the API key endpoint converts it to the form
    sessions actually need (ANTHROPIC_API_KEY).
    """
    async with httpx.AsyncClient(timeout=_API_KEY_TIMEOUT) as client:
        resp = await client.post(
            API_KEY_URL,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            json={},
        )
    if resp.status_code not in (200, 201):
        snippet = resp.text[:300]
        raise RuntimeError(
            f"api-key endpoint returned {resp.status_code}: {snippet}"
        )
    body = resp.json()
    # Schema observed: {"raw_key": "sk-ant-...", ...}. Be defensive about
    # the exact field name in case it varies.
    for key in ("raw_key", "api_key", "key", "value"):
        v = body.get(key)
        if isinstance(v, str) and v.startswith("sk-ant-"):
            return v
    raise RuntimeError(
        f"api-key response didn't include a sk-ant- key; keys={list(body.keys())}"
    )


# Singleton — wired into the FastAPI lifespan in main.py.
oauth_login_manager = OAuthLoginManager()
