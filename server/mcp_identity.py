"""Per-session identity for the HTTP-served MCP tool namespaces.

The stdio sidecars each received their identity as process environment at
spawn time. A single HTTP endpoint serving every session has no per-process
environment, so identity has to travel with the request instead.

It travels in the bearer, not in a custom header. Claude Code accepts
arbitrary per-server headers; Codex does not — its `--env` is documented
stdio-only and the sole per-server hook for an HTTP server names an env var
to read a bearer from. A header-based scheme would therefore work on one
backend and be impossible on the other, which is exactly the kind of
capability difference `server/harness/` exists to keep out of feature code.

The shape follows `applications.app_scope_token`: derived rather than stored,
so there is no new secret to persist, back up or leak, and rotating
`OCTOPUS_AUTH_TOKEN` rotates every scope with it (token-rotation.md).
"""

from __future__ import annotations

import hashlib
import hmac
from contextvars import ContextVar
from dataclasses import dataclass

from .config import settings

# Set per request by the ASGI middleware below and read by the tool bodies.
# A ContextVar rather than a parameter because the MCP SDK owns the call path
# between the HTTP request and the tool function, so there is nowhere to thread
# an argument through; and a ContextVar is task-local, so concurrent turns from
# different sessions cannot observe each other's scope.
_current: ContextVar[McpScope | None] = ContextVar("octopus_mcp_scope", default=None)


@dataclass(frozen=True)
class McpScope:
    """Who a tool call belongs to: always a session, plus a connector
    installation when the namespace is a connector's."""

    session_id: str
    installation_id: str | None = None


def _sign(payload: str) -> str:
    return hmac.new(
        settings.auth_token.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def _payload(scope: McpScope) -> str:
    return f"mcp:{scope.session_id}:{scope.installation_id or ''}"


def mint(session_id: str, installation_id: str | None = None) -> str:
    """The bearer for one session (and one connector installation, if any).

    The scope is carried in the clear alongside its signature so the server can
    tell *which* session is calling without a lookup table, while the signature
    is what makes the claim trustworthy. Nothing here is stored.
    """
    scope = McpScope(session_id, installation_id)
    return f"{scope.session_id}.{scope.installation_id or ''}.{_sign(_payload(scope))}"


def verify(bearer: str | None) -> McpScope | None:
    """The scope a bearer proves, or None if it proves nothing.

    Returns None rather than raising: an unverifiable bearer is an ordinary
    "not authorised" outcome on a public HTTP surface, not an exception.
    """
    if not bearer:
        return None
    parts = bearer.split(".")
    if len(parts) != 3:
        return None
    session_id, installation_id, signature = parts
    if not session_id:
        return None
    scope = McpScope(session_id, installation_id or None)
    if not hmac.compare_digest(signature, _sign(_payload(scope))):
        return None
    return scope


def current_scope() -> McpScope | None:
    """The scope of the request being served, or None outside one."""
    return _current.get()


def set_current_scope(scope: McpScope | None):
    """Bind the scope for this task. Returns the ContextVar token so the caller
    can restore the previous value."""
    return _current.set(scope)


def reset_current_scope(token) -> None:  # type: ignore[no-untyped-def]
    _current.reset(token)
