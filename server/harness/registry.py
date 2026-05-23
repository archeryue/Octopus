"""Backend-kind → Harness registry.

The two profiles register a `Harness` on import (mirrors the connector
registry). Feature code resolves a harness with `get_harness(backend)`;
`available_backends()` (CLI-resolvable kinds) feeds the frontend picker
and `main.py`'s availability probe.
"""

from __future__ import annotations

from .harness import Harness

_REGISTRY: dict[str, Harness] = {}

# The default backend when a kind isn't specified (legacy rows / tests).
DEFAULT_BACKEND = "claude-code"


def register(harness: Harness) -> None:
    _REGISTRY[harness.backend] = harness


def get_harness(backend: str | None) -> Harness:
    """Resolve a backend kind to its Harness. None → the default kind.
    Raises ValueError on an unknown kind (explicit, not a silent fallback)."""
    key = backend or DEFAULT_BACKEND
    harness = _REGISTRY.get(key)
    if harness is None:
        raise ValueError(f"Unknown backend: {backend!r}")
    return harness


def has_backend(backend: str | None) -> bool:
    return (backend or DEFAULT_BACKEND) in _REGISTRY


def all_backends() -> list[str]:
    return list(_REGISTRY.keys())


def available_backends() -> list[str]:
    """Registered kinds whose CLI resolves on this host."""
    return [b for b, h in _REGISTRY.items() if h.is_available()]
