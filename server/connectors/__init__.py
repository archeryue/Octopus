"""Connectors package (connectors.md).

Importing this package registers every connector kind in KIND_REGISTRY. Kind
modules (github, gmail) call `registry.register(...)` at import time; they're
imported here so a single `import server.connectors` populates the registry.
"""

from __future__ import annotations

# Kind modules self-register on import.
from . import (
    github,  # noqa: E402,F401  (Phase B)
    gmail,  # noqa: E402,F401  (Phase C)
)
from .base import MAX_TOOL_NAME_LEN, ConnectorBase, ConnectorInstallation, HealthStatus
from .oauth import (
    ConnectorLoginError,
    ConnectorLoginManager,
    ConnectorLoginState,
    ConnectorOAuthProvider,
    PendingLogin,
)
from .registry import KIND_REGISTRY, all_connectors, get_connector, register

__all__ = [
    "ConnectorBase",
    "ConnectorInstallation",
    "HealthStatus",
    "MAX_TOOL_NAME_LEN",
    "ConnectorOAuthProvider",
    "ConnectorLoginManager",
    "ConnectorLoginState",
    "ConnectorLoginError",
    "PendingLogin",
    "KIND_REGISTRY",
    "register",
    "get_connector",
    "all_connectors",
]
