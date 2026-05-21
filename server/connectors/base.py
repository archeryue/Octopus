"""ConnectorBase + ConnectorInstallation (connectors.md §5.2).

A connector is a stateless descriptor for one third-party "kind" (gmail,
github, …): its OAuth config, its tool verbs, and how to spawn its MCP server.
Concrete kinds subclass this and live in `server/connectors/<kind>.py`; the
matching stdio MCP server lives in `server/mcp_servers/connectors/<kind>.py`.

`mcp_entry` returns a backend-neutral `{command, args, env}` so both backends
render it into their own MCP config shape (Claude `--mcp-config` JSON, Codex
`-c mcp_servers.*` overrides) without per-backend connector code.
"""

from __future__ import annotations

import abc
import sys
from dataclasses import dataclass, field
from typing import Any

from ..oauth_providers import OAuthTokenSet
from .oauth import ConnectorOAuthProvider


@dataclass
class ConnectorInstallation:
    """A view of one installed account, as passed to a connector's methods."""

    id: str
    kind: str
    label: str
    external_account_id: str | None = None
    scopes: list[str] = field(default_factory=list)
    needs_reconnect: bool = False

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "ConnectorInstallation":
        return cls(
            id=row["id"],
            kind=row["kind"],
            label=row["label"],
            external_account_id=row.get("external_account_id"),
            scopes=list(row.get("scopes") or []),
            needs_reconnect=bool(row.get("needs_reconnect")),
        )


@dataclass
class HealthStatus:
    ok: bool
    needs_reconnect: bool = False
    detail: str | None = None


def render_connectors_blurb(
    connectors: list[tuple["ConnectorBase", "ConnectorInstallation"]],
) -> str:
    """The `== Connectors ==` system-prompt section (connectors.md §5.8) for a
    list of (connector, installation) tuples. Shared by both backends."""
    blurbs = "\n\n".join(c.system_prompt_blurb(i) for c, i in connectors)
    return (
        "== Connectors ==\n\n"
        "You also have access to the following third-party connectors. Treat "
        "them as first-class tools — call them whenever the request involves "
        f"the linked account.\n\n{blurbs}"
    )


# Tool names hit the model's 64-char limit; `mcp__<kind>_<6>__` already eats
# ~13. Keep verbs short, snake_case. Enforced by a test.
MAX_TOOL_NAME_LEN = 60


class ConnectorBase(abc.ABC):
    kind: str  # 'gmail' | 'github' — matches the MCP module + DB `kind`
    display_name: str  # 'Gmail'
    category: str = "other"  # for grouping in the catalog picker
    allows_multiple: bool = False  # multiple installs of the same kind?
    oauth: ConnectorOAuthProvider
    # Verb names this connector's MCP server exposes (without the
    # `mcp__<key>__` prefix the CLI adds). Drives the system-prompt blurb and
    # the tool-name-length invariant.
    tools: tuple[str, ...] = ()
    # One-line intro for the system-prompt section; override per kind.
    blurb_intro: str = ""
    # In-app setup guidance (shown in the "Set up" dialog so a browser-only
    # user knows how to register the OAuth app): a link to the provider's
    # app-registration page + ordered steps.
    setup_url: str = ""
    setup_steps: tuple[str, ...] = ()

    @property
    def mcp_module(self) -> str:
        """The python module path of this kind's stdio MCP server."""
        return f"server.mcp_servers.connectors.{self.kind}"

    def mcp_key(self, installation: ConnectorInstallation) -> str:
        """The `mcpServers` key for one installation. Includes a short id
        slice so two accounts of the same kind don't collide."""
        return f"{self.kind}_{installation.id[:6]}"

    def tool_name(self, installation: ConnectorInstallation, verb: str) -> str:
        """The full tool name the model sees, e.g. `mcp__gmail_4a2f__search`."""
        return f"mcp__{self.mcp_key(installation)}__{verb}"

    def mcp_entry(
        self,
        installation: ConnectorInstallation,
        callback_env: dict[str, str],
    ) -> dict[str, Any]:
        """Backend-neutral spawn spec for this installation's MCP server.

        `callback_env` is the shared MCP env (OCTOPUS_API_BASE / _AUTH_TOKEN /
        _SESSION_ID / PYTHONPATH); we add OCTOPUS_INSTALLATION_ID so the server
        knows which installation's token to fetch.
        """
        return {
            "command": sys.executable,
            "args": ["-m", self.mcp_module],
            "env": {
                **callback_env,
                "OCTOPUS_INSTALLATION_ID": installation.id,
            },
        }

    def system_prompt_blurb(self, installation: ConnectorInstallation) -> str:
        """The `== Connectors ==` section entry appended to the system prompt
        so the model knows the tools exist."""
        header = f"[{self.kind} / {installation.label}]"
        intro = f"\n{self.blurb_intro}" if self.blurb_intro else ""
        lines = "\n".join(
            f"  {self.tool_name(installation, verb)}" for verb in self.tools
        )
        return f"{header}{intro}\n{lines}"

    async def fetch_external_identity(
        self, token_set: OAuthTokenSet
    ) -> tuple[str, str]:
        """After OAuth, call the provider's profile endpoint for a stable
        (external_account_id, label) pair. Override per kind."""
        raise NotImplementedError

    async def health_check(
        self, installation: ConnectorInstallation, token: str
    ) -> HealthStatus:
        """Optional cheap liveness probe. Default assumes healthy; kinds with
        a profile endpoint override and flip needs_reconnect on 401."""
        return HealthStatus(ok=True)
