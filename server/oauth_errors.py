"""Typed refresh-error codes — Steal Plan B-5.

These are the categories of failure that can come back from an OAuth
token refresh, used by:

  - `backend_credentials.last_refresh_error_code` on the DB row
  - the REST `CredentialInfo.last_refresh_error_code` field surfaced to
    the frontend

The frontend uses the code to decide whether to nudge the user toward
"sign in again" (`refresh_token_*` cases) or "we'll retry"
(transient / unknown). Claude Code's current `sk-ant-` flow doesn't
refresh, so the column stays null in practice today — the schema is in
place so the next OAuth provider (Codex / GitHub / Lark) with refresh
tokens has somewhere to write its diagnoses.
"""

from __future__ import annotations

from enum import Enum


class RefreshErrorCode(str, Enum):
    refresh_token_expired = "refresh_token_expired"
    refresh_token_reused = "refresh_token_reused"
    refresh_token_invalidated = "refresh_token_invalidated"
    refresh_token_other = "refresh_token_other"
    network_error = "network_error"
    unknown = "unknown"


# Allowed string values for Pydantic / TypeScript boundary use.
REFRESH_ERROR_CODES: tuple[str, ...] = tuple(c.value for c in RefreshErrorCode)
