"""The deep-research pipeline (native-deep-research.md §3), orchestrated in
Python: scope → parallel angle-search → dedup/rank → adversarial verify →
synthesize. Every model call goes through an injectable async callable
(`search` for web leaves, `reason` for tool-free oneshots) — defaulting to the
real leaf executors — so the whole pipeline is unit-testable with fakes (the
`runner=`-injection pattern schedule_ai uses). One semaphore bounds ALL
concurrent leaves so a job never spawns the ~75-subprocess storm.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from .leaf import LeafResult, run_reason_leaf, run_web_leaf
from .schemas import (
    Finding,
    dedup_and_rank,
    parse_angles,
    parse_findings,
    parse_verdict,
    scope_prompt,
    search_prompt,
    synthesize_prompt,
    verify_prompt,
)

logger = logging.getLogger(__name__)

# A leaf callable takes a prompt and returns a LeafResult. Two flavors: `search`
# (a web sub-turn) and `reason` (a tool-free oneshot).
LeafCallable = Callable[[str], Awaitable[LeafResult]]
ProgressCallback = Callable[["ResearchProgress"], Awaitable[None]]


@dataclass
class ResearchLimits:
    max_angles: int = 5
    max_findings_per_angle: int = 6
    max_claims: int = 12
    votes_per_claim: int = 2          # kill a claim only on a strict-majority refute
    concurrency: int = 4              # cap on simultaneous leaves within a job
    leaf_timeout: float = 150.0       # per web sub-turn
    reason_timeout: float = 90.0      # per oneshot


@dataclass
class ResearchProgress:
    phase: str                        # scope | search | verify | synthesize | done
    detail: str
    counts: dict[str, int] = field(default_factory=dict)


@dataclass
class ResearchReport:
    question: str
    report: str
    findings: list[Finding]
    sources: list[str]
    angles: list[str]
    claims_examined: int
    # Best-effort USD: the sum of per-leaf costs the backend reports. Only
    # WEB leaves report cost (Claude `result.total_cost_usd`); the tool-free
    # reasoning leaves go through `run_oneshot`, which returns only text, so
    # scope/synthesis cost is NOT included. Codex reports no USD at all → often
    # None. Treat as a partial lower bound, not a total (Vera review).
    cost: float | None = None


async def run_research(
    question: str,
    *,
    harness: Any = None,
    credential: Any = None,
    model: str | None = None,
    working_dir: str,
    limits: ResearchLimits | None = None,
    on_progress: ProgressCallback | None = None,
    search: LeafCallable | None = None,
    reason: LeafCallable | None = None,
) -> ResearchReport:
    """Run the full pipeline. Provide `search`/`reason` to inject leaf behavior
    (tests); otherwise they default to the real executors bound to `harness` +
    `credential` + `model` + `working_dir` (so a real call needs a harness)."""
    limits = limits or ResearchLimits()
    sem = asyncio.Semaphore(max(1, limits.concurrency))
    cost = 0.0

    if search is None:
        async def search(p: str) -> LeafResult:  # noqa: E306
            return await run_web_leaf(
                harness, prompt=p, working_dir=working_dir, credential=credential,
                model=model, timeout=limits.leaf_timeout,
            )
    if reason is None:
        async def reason(p: str) -> LeafResult:  # noqa: E306
            return await run_reason_leaf(
                harness, prompt=p, credential=credential, model=model,
                working_dir=working_dir, timeout=limits.reason_timeout,
            )

    async def _emit(phase: str, detail: str, **counts: int) -> None:
        if on_progress is not None:
            await on_progress(ResearchProgress(phase=phase, detail=detail, counts=counts))

    async def _bounded(coro: Awaitable[LeafResult]) -> LeafResult:
        async with sem:
            return await coro

    # 1. Scope -----------------------------------------------------------------
    await _emit("scope", "Decomposing the question into search angles…")
    scope_res = await reason(scope_prompt(question, limits.max_angles))
    cost += scope_res.cost or 0.0
    angles = parse_angles(scope_res.text, question=question, max_angles=limits.max_angles)

    # 2. Search (parallel web leaves, bounded) ---------------------------------
    await _emit("search", f"Searching {len(angles)} angle(s)…", angles=len(angles))
    search_results = await asyncio.gather(
        *(_bounded(search(search_prompt(a, question, limits.max_findings_per_angle)))
          for a in angles)
    )
    findings: list[Finding] = []
    for angle, res in zip(angles, search_results):
        cost += res.cost or 0.0
        findings.extend(
            parse_findings(res.text, angle=angle, max_findings=limits.max_findings_per_angle)
        )

    # 3. Dedup + rank (pure) ---------------------------------------------------
    claims = dedup_and_rank(findings, max_claims=limits.max_claims)

    # 4. Verify (K adversarial votes each; strict-majority refute kills) -------
    await _emit("verify", f"Verifying {len(claims)} claim(s)…",
                claims=len(claims), gathered=len(findings))
    kill_threshold = limits.votes_per_claim // 2 + 1

    async def _verify(f: Finding) -> tuple[bool, float]:
        votes = await asyncio.gather(
            *(_bounded(search(verify_prompt(f.claim, f.url, question)))
              for _ in range(limits.votes_per_claim))
        )
        refutes = sum(1 for v in votes if not v.error and parse_verdict(v.text))
        leaf_cost = sum(v.cost or 0.0 for v in votes)
        return refutes < kill_threshold, leaf_cost

    survivors: list[Finding] = []
    if claims:
        verdicts = await asyncio.gather(*(_verify(f) for f in claims))
        for f, (survived, c) in zip(claims, verdicts):
            cost += c
            if survived:
                survivors.append(f)

    # 5. Synthesize ------------------------------------------------------------
    await _emit("synthesize", f"Writing the report from {len(survivors)} verified finding(s)…",
                verified=len(survivors))
    synth_res = await reason(synthesize_prompt(question, survivors))
    cost += synth_res.cost or 0.0
    report_text = synth_res.text or "(the model produced no report)"

    sources = sorted({f.url for f in survivors if f.url})
    await _emit("done", "Research complete.",
                verified=len(survivors), sources=len(sources))
    return ResearchReport(
        question=question,
        report=report_text,
        findings=survivors,
        sources=sources,
        angles=angles,
        claims_examined=len(claims),
        cost=cost or None,
    )
