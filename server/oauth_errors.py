"""Typed errors + refresh-error codes for the OAuth credential flow.

`RefreshErrorCode` categorizes failures from an OAuth access-token refresh
and is written to:
  - `backend_credentials.last_refresh_error_code` on the DB row
  - the REST `CredentialInfo.last_refresh_error_code` field surfaced to
    the frontend

The frontend uses the code to decide whether to nudge the user toward
"sign in again" (`refresh_token_*` cases) or "we'll retry"
(transient / unknown).

`ScopeMissingError` is raised by `mint_api_key()` when the OAuth token
wasn't granted the scope required to create a long-lived API key. The
login orchestrator catches it and falls back to storing the OAuth
access+refresh tokens themselves (the "subscription auth" path the
Claude CLI uses for Pro/Max users without an org).
"""

from __future__ import annotations

from enum import Enum


class RefreshErrorCode(str, Enum):
    refresh_token_expired = "refresh_token_expired"
    refresh_token_reused = "refresh_token_reused"
    refresh_token_invalidated = "refresh_token_invalidated"
    refresh_token_other = "refresh_token_other"
    network_error = "network_error"
    # The CLI rejected the credential at runtime with a 401 / auth error,
    # detected reactively from a failed turn rather than from a proactive
    # token refresh (harness-credential-reauth.md §4).
    invalid_credentials = "invalid_credentials"
    unknown = "unknown"


# Allowed string values for Pydantic / TypeScript boundary use.
REFRESH_ERROR_CODES: tuple[str, ...] = tuple(c.value for c in RefreshErrorCode)


class ScopeMissingError(RuntimeError):
    """OAuth token lacks the scope needed for the requested action.

    Surfaces specifically when the api-key endpoint returns 403 with the
    `org:create_api_key` scope error — typical for Pro/Max subscribers
    without an Anthropic org. The login manager catches this and stores
    the OAuth access/refresh tokens directly instead of an API key.
    """

    def __init__(self, message: str, missing_scope: str) -> None:
        super().__init__(message)
        self.missing_scope = missing_scope
