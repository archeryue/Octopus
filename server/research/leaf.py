"""Research leaf executors (native-deep-research.md §4–5).

A "leaf" is one fan-out unit:
  - WEB leaf (`run_web_leaf`): an isolated, scoped `HarnessRun` sub-turn that
    uses the backend's native web tools (read-only) to search/read and return
    text. It carries NO inherited MCP/connectors/memory, a scratch cwd, a
    minimal system prompt, and `web_research=True` (the profile renders a
    web-enabled, destructive-tool-free turn) — so it can't touch the user's
    session or the host.
  - REASONING leaf (`run_reason_leaf`): a tool-free `run_oneshot` (scope
    decompose, synthesize) — the same agnostic primitive schedule_ai uses.

Both return a `LeafResult` and never raise: a failed leaf degrades to
`error` text so the orchestrator can carry on. Backend-agnostic.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from ..harness import HarnessCredential, HarnessOneshotError, OneShotContext, RunConfig

logger = logging.getLogger(__name__)

# Minimal persona for a web leaf — it has no Octopus in-app tools, just the
# backend's web tools; the per-leaf user prompt carries the real instruction.
_LEAF_SYSTEM = (
    "You are a focused web-research worker. Use ONLY your web tools to do what "
    "the user asks, then reply with exactly the format requested — no preamble."
)


@dataclass
class LeafResult:
    """One leaf's outcome. `text` is the model's final reply (empty on error);
    `cost` is USD when the backend reports it; `error` is set on any failure."""

    text: str
    cost: float | None = None
    error: str | None = None


async def run_web_leaf(
    harness: Any,
    *,
    prompt: str,
    working_dir: str,
    credential: HarnessCredential | None,
    model: str | None,
    timeout: float = 120.0,
) -> LeafResult:
    """Run one isolated, web-enabled, read-only-ish sub-turn and return its
    final text. Side-effect-contained: empty MCP set, no connectors/memory,
    `web_research=True`. Bounded by `timeout`; the process group is reaped on
    stop (turn-safety.md §2)."""
    config = RunConfig(
        session_id=None,        # no callback env → no bg/ask/ask_agent wiring
        system_prompt=_LEAF_SYSTEM,
        model=model,
        mcp_servers=[],         # [] (not None) = NO servers (None = all defaults)
        tool_allow=None,
        tool_deny=None,
        connectors=[],
        memory_dir=None,
        web_research=True,
    )
    run = harness.create_run(config)
    parts: list[str] = []
    cost = 0.0
    error: str | None = None
    try:
        await run.start(prompt, working_dir, None, credential=credential)

        async def _consume() -> None:
            nonlocal cost, error
            async for ev in run.stream():
                if ev.type == "text" and ev.content:
                    parts.append(ev.content)
                elif ev.type == "result":
                    if ev.cost:
                        cost += ev.cost
                    if ev.is_error:
                        error = error or (ev.content or "web leaf failed")
                elif ev.type == "error" and ev.is_error:
                    error = error or (ev.content or "web leaf error")

        await asyncio.wait_for(_consume(), timeout=timeout)
    except asyncio.TimeoutError:
        error = "web leaf timed out"
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001 — a leaf must never crash the job
        logger.exception("web research leaf crashed")
        error = str(e)
    finally:
        try:
            await run.stop()
        except Exception:
            logger.exception("web research leaf stop() failed")
    return LeafResult(text="".join(parts).strip(), cost=cost or None, error=error)


async def run_reason_leaf(
    harness: Any,
    *,
    prompt: str,
    credential: HarnessCredential | None,
    model: str | None,
    working_dir: str | None,
    timeout: float = 90.0,
) -> LeafResult:
    """Run one tool-free reasoning call (`run_oneshot`) — scope decompose /
    synthesize. Backend-agnostic; never raises."""
    ctx = OneShotContext(
        prompt=prompt, model=model, credential=credential, working_dir=working_dir
    )
    try:
        text = await harness.run_oneshot(ctx, timeout=timeout)
        return LeafResult(text=(text or "").strip())
    except HarnessOneshotError as e:
        return LeafResult(text="", error=f"reasoning leaf failed: {e.code}")
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001
        logger.exception("reasoning leaf crashed")
        return LeafResult(text="", error=str(e))
