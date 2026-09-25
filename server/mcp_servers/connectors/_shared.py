"""Shared plumbing for connector MCP servers (connectors.md §5.3).

Each connector MCP server runs as a grandchild of the FastAPI process and
calls back to it for the per-installation access token. Tokens are cached
in-process until just before expiry so we don't HTTP every tool call. Results
are capped so a large payload can't trip the CLI premature-exit bug
(docs/post-mortems/2026-05-18-bg-pipeline-hardening.md §2).
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import httpx

from .. import _host

logger = logging.getLogger(__name__)

# Hard cap per tool result. Above this the CLI may silently drop the
# tool_result event; we truncate and tell the model to narrow/paginate.
MAX_RESULT_BYTES = 32 * 1024
_HTTP_TIMEOUT = 10.0


class ConnectorContext:
    """Reads the host callback env and brokers the installation's token.

    Env (api base / auth token / installation id) is read lazily so tests can
    set it after import; only the fetched token is cached on the instance.
    """

    def __init__(self) -> None:
        self._access_token: str | None = None
        self._token_exp: float = 0.0

    # Resolved per call from the request's verified scope and from settings.
    # These used to read the process environment, which worked when each
    # installation had its own sidecar; served in-process one module handles
    # every installation, so the installation has to come from the call
    # (polish-2026-09.md §4 B1).
    @property
    def api_base(self) -> str | None:
        return _host.resolve("OCTOPUS_API_BASE")

    @property
    def auth_token(self) -> str | None:
        return _host.resolve("OCTOPUS_AUTH_TOKEN")

    @property
    def installation_id(self) -> str | None:
        return _host.resolve("OCTOPUS_INSTALLATION_ID")

    def ready(self) -> bool:
        return bool(self.api_base and self.auth_token and self.installation_id)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.auth_token}"}

    def access_token(self) -> str | None:
        """Fresh access token for this installation, or None if unavailable.
        Caches until ~30s before expiry; non-expiring tokens cache for 1h."""
        now = time.time()
        if self._access_token and now < self._token_exp - 30:
            return self._access_token
        if not self.ready():
            logger.error("connector MCP misconfigured: missing callback env")
            return None
        try:
            r = httpx.get(
                f"{self.api_base}/api/connectors/{self.installation_id}/token",
                headers=self._headers(),
                timeout=_HTTP_TIMEOUT,
            )
        except httpx.HTTPError as e:
            logger.warning("token fetch failed: %s", e)
            return None
        if r.status_code != 200:
            logger.warning("token fetch HTTP %s", r.status_code)
            _record_connector(self.installation_id, ok=False,
                              error_code=f"token_http_{r.status_code}")
            return None
        body = r.json()
        self._access_token = body["access_token"]
        exp = body.get("expires_at_epoch") or 0.0
        self._token_exp = exp if exp else now + 3600
        return self._access_token

    def mark_needs_reconnect(self, error_code: str = "invalid_grant") -> None:
        if not self.ready():
            return
        try:
            httpx.post(
                f"{self.api_base}/api/connectors/{self.installation_id}"
                "/mark-needs-reconnect",
                params={"error_code": error_code},
                headers=self._headers(),
                timeout=_HTTP_TIMEOUT,
            )
        except httpx.HTTPError as e:
            logger.warning("mark-needs-reconnect failed: %s", e)
        # Drop the cached token so the next call re-fetches (and 401s cleanly).
        self._access_token = None
        self._token_exp = 0.0


def _record_connector(installation_id, *, ok: bool, error_code: str | None = None,
                      duration_ms: float | None = None) -> None:
    """Count a connector call. This is the hook the Gmail outage needed: the
    connector was unavailable for eleven consecutive days, a schedule failed on
    each of them, and nothing anywhere recorded the repetition
    (polish-2026-09.md §9 G0)."""
    from ...mcp_identity import current_scope
    from ...monitor import Event, record

    scope = current_scope()
    record(Event(
        kind="connector_call",
        session_id=scope.session_id if scope else None,
        duration_ms=duration_ms,
        ok=ok,
        error_code=error_code,
        detail={"installation_id": installation_id},
    ))


def truncate(text: str, cap: int = MAX_RESULT_BYTES) -> str:
    raw = text.encode("utf-8")
    if len(raw) <= cap:
        return text
    head = raw[:cap].decode("utf-8", "ignore")
    return (
        f"{head}\n…[truncated {len(raw) - cap} bytes — narrow your query or "
        "request a specific item]"
    )


def to_text(obj: Any) -> str:
    if isinstance(obj, str):
        return obj
    return json.dumps(obj, indent=2, default=str, ensure_ascii=False)
