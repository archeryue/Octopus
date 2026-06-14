"""Octopus-native deep research (native-deep-research.md).

Octopus owns the fan-out orchestration; each harness's OWN native web tools do
the searching/fetching (we never build search). Backend-agnostic: the
orchestrator only ever calls `run_oneshot` (reasoning leaves) and a scoped,
isolated `HarnessRun` web sub-turn (web leaves) — no `if backend ==`.
"""

from .leaf import LeafResult, run_reason_leaf, run_web_leaf
from .manager import ResearchError, ResearchManager, research_manager
from .orchestrator import (
    ResearchLimits,
    ResearchProgress,
    ResearchReport,
    run_research,
)

__all__ = [
    "LeafResult",
    "run_web_leaf",
    "run_reason_leaf",
    "ResearchLimits",
    "ResearchProgress",
    "ResearchReport",
    "run_research",
    "ResearchError",
    "ResearchManager",
    "research_manager",
]
