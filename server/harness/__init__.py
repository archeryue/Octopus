"""The harness layer — the single boundary for all model/runtime interaction.

One `Harness` class configured by a `RuntimeProfile` value per backend
kind (no per-framework subclasses). See docs/plan/harness-layer.md.

Profiles self-register on import; importing this package wires them up
(the claude_code/codex imports below run their `register(...)` calls).
"""

from __future__ import annotations

from .events import HarnessCredential, HarnessEvent, HarnessOneshotError
from .harness import Harness
from .login import LoginDriver, LoginMethod
from .profile import (
    EventParser,
    McpServerEntry,
    OneShotContext,
    ParseOutput,
    RuntimeProfile,
    TranscriptCodec,
    TurnContext,
)
from .registry import (
    DEFAULT_BACKEND,
    all_backends,
    available_backends,
    get_harness,
    has_backend,
    register,
)
from .run import HarnessRun, RunConfig

__all__ = [
    "HarnessCredential",
    "HarnessEvent",
    "HarnessOneshotError",
    "Harness",
    "HarnessRun",
    "RunConfig",
    "RuntimeProfile",
    "TurnContext",
    "OneShotContext",
    "McpServerEntry",
    "EventParser",
    "ParseOutput",
    "TranscriptCodec",
    "LoginDriver",
    "LoginMethod",
    "register",
    "get_harness",
    "has_backend",
    "all_backends",
    "available_backends",
    "DEFAULT_BACKEND",
]

# Profile registration: importing the profile modules runs their
# `register(Harness(...))` side effects.
from . import claude_code  # noqa: E402,F401  (registers the claude-code harness)
from . import codex  # noqa: E402,F401  (registers the codex harness)
