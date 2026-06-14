"""Unit tests for the deep-research pipeline + schemas (native-deep-research.md).

Everything runs with INJECTED leaf callables (the schedule_ai `runner=` pattern),
so there's no network, no CLI, no backend — just the orchestration logic.
"""

from __future__ import annotations

import asyncio

import pytest

from server.research.leaf import LeafResult
from server.research.orchestrator import ResearchLimits, run_research
from server.research import schemas


# --------------------------------------------------------------- schemas (pure)


def test_parse_angles_tolerates_fences_and_falls_back():
    assert schemas.parse_angles(
        '```json\n["a", "b", "c"]\n```', question="q", max_angles=5
    ) == ["a", "b", "c"]
    # objects with an "angle"/"query" key also work
    assert schemas.parse_angles(
        '[{"query": "x"}, {"angle": "y"}]', question="q", max_angles=5
    ) == ["x", "y"]
    # garbage → fall back to the question
    assert schemas.parse_angles("not json", question="the q", max_angles=5) == ["the q"]
    # cap
    assert schemas.parse_angles("[\"1\",\"2\",\"3\"]", question="q", max_angles=2) == ["1", "2"]


def test_parse_findings_and_verdict():
    fs = schemas.parse_findings(
        '[{"claim":"A","url":"http://a"},{"claim":"B","source":"http://b"}]',
        angle="ang", max_findings=5,
    )
    assert [(f.claim, f.url, f.angle) for f in fs] == [
        ("A", "http://a", "ang"), ("B", "http://b", "ang")
    ]
    assert schemas.parse_verdict('{"refuted": true}') is True
    assert schemas.parse_verdict('{"refuted": false}') is False
    assert schemas.parse_verdict("unparseable") is False  # default: not refuted


def test_dedup_and_rank_prefers_cross_angle_corroboration():
    F = schemas.Finding
    findings = [
        F("The sky is blue", "u1", "a1"),
        F("the sky is blue.", "u2", "a2"),   # dup of the first (normalized)
        F("Water is wet", "u3", "a1"),
    ]
    ranked = schemas.dedup_and_rank(findings, max_claims=10)
    # "sky is blue" appeared in 2 angles → ranks first; dup collapsed.
    assert [f.claim for f in ranked] == ["The sky is blue", "Water is wet"]


# --------------------------------------------------------------- full pipeline


@pytest.mark.asyncio
async def test_run_research_happy_path_phases_and_verification():
    phases: list[str] = []

    async def on_progress(p):
        phases.append(p.phase)

    async def fake_reason(prompt: str) -> LeafResult:
        if "Decompose" in prompt:
            return LeafResult(text='["angle one", "angle two"]', cost=0.01)
        # synthesize
        return LeafResult(text="# Report\nThe answer is 42 [http://a].", cost=0.02)

    async def fake_search(prompt: str) -> LeafResult:
        if "Fact-check" in prompt:
            # refute anything mentioning "bad", else keep
            refuted = "bad claim" in prompt
            return LeafResult(text=f'{{"refuted": {str(refuted).lower()}}}', cost=0.001)
        # search angle → return findings
        return LeafResult(
            text='[{"claim":"good claim","url":"http://a"},'
                 '{"claim":"bad claim","url":"http://b"}]',
            cost=0.003,
        )

    report = await run_research(
        "What is the answer?",
        working_dir="/tmp",
        limits=ResearchLimits(votes_per_claim=1, concurrency=3),
        on_progress=on_progress,
        search=fake_search,
        reason=fake_reason,
    )

    assert phases == ["scope", "search", "verify", "synthesize", "done"]
    assert report.angles == ["angle one", "angle two"]
    # "good claim" survives; "bad claim" refuted (1 vote, kill_threshold=1) → dropped
    claims = {f.claim for f in report.findings}
    assert "good claim" in claims and "bad claim" not in claims
    assert report.sources == ["http://a"]
    assert "Report" in report.report
    assert report.cost and report.cost > 0


@pytest.mark.asyncio
async def test_run_research_respects_concurrency_cap():
    live = 0
    peak = 0

    async def fake_reason(prompt: str) -> LeafResult:
        if "Decompose" in prompt:
            return LeafResult(text='["a","b","c","d","e"]')
        return LeafResult(text="report")

    async def fake_search(prompt: str) -> LeafResult:
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        await asyncio.sleep(0.02)
        live -= 1
        return LeafResult(text='[{"claim":"c","url":"u"}]')

    await run_research(
        "q", working_dir="/tmp",
        limits=ResearchLimits(max_angles=5, votes_per_claim=1, concurrency=2),
        search=fake_search, reason=fake_reason,
    )
    assert peak <= 2, f"concurrency cap exceeded: peak={peak}"


@pytest.mark.asyncio
async def test_run_research_survives_leaf_errors():
    async def fake_reason(prompt: str) -> LeafResult:
        if "Decompose" in prompt:
            return LeafResult(text="garbage")  # → falls back to [question]
        return LeafResult(text="final report")

    async def fake_search(prompt: str) -> LeafResult:
        return LeafResult(text="", error="leaf timed out")  # every leaf fails

    report = await run_research(
        "my question", working_dir="/tmp",
        limits=ResearchLimits(votes_per_claim=1),
        search=fake_search, reason=fake_reason,
    )
    # No findings (all searches errored), but the job completes with a report.
    assert report.findings == []
    assert report.angles == ["my question"]
    assert report.report == "final report"
