"""KIND_REGISTRY — the registered connector kinds (connectors.md §5.1).

Connector kind modules register their singleton here at import time; the
registry is populated when `server.connectors` is imported (see __init__).
Mirrors the `PROVIDERS` dict in `server/oauth_providers.py` (instances, not
classes — connectors are stateless).
"""

from __future__ import annotations

from .base import ConnectorBase

KIND_REGISTRY: dict[str, ConnectorBase] = {}


def register(connector: ConnectorBase) -> None:
    KIND_REGISTRY[connector.kind] = connector


def get_connector(kind: str) -> ConnectorBase | None:
    return KIND_REGISTRY.get(kind)


def all_connectors() -> list[ConnectorBase]:
    return list(KIND_REGISTRY.values())
