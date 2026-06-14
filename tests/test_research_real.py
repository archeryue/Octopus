"""Real end-to-end deep research (native-deep-research.md).

Drives the WHOLE backend stack — ResearchManager → orchestrator → leaf
executors → a REAL harness with REAL web tools — and asserts a cited report
comes back and is injected. Gated on codex being signed in (it has web search
via `tools.web_search`); skipped otherwise so CI without a login still passes.
Costs real API calls + a minute or two.

Limits are shrunk hard (1 angle, 1 claim, 1 vote) to keep it quick.
"""

from __future__ import annotations

import asyncio

import pytest

from tests.cli_gate import codex_cli_works
from server.database import Database
from server.research import manager as rm_mod
from server.research.manager import ResearchManager
from server.research.orchestrator import ResearchLimits, run_research as _real_run
from server.session_manager import SessionManager

pytestmark = pytest.mark.skipif(
    not codex_cli_works(), reason="codex CLI unavailable or not signed in"
)


@pytest.mark.asyncio
async def test_real_codex_deep_research_end_to_end(tmp_path, monkeypatch):
    db = Database(":memory:")
    await db.initialize()
    mgr = SessionManager()
    await mgr.initialize(db)
    rm = ResearchManager()
    rm.bind(session_mgr=mgr, db=db)

    # Capture the injected report instead of spawning a real follow-up turn.
    injected: list[str] = []

    async def fake_start_message(session_id, prompt):
        injected.append(prompt)

    monkeypatch.setattr(mgr, "start_message", fake_start_message)

    # Shrink the pipeline for speed (still real searches).
    async def small_run(question, **kw):
        kw["limits"] = ResearchLimits(
            max_angles=1, max_findings_per_angle=2, max_claims=1,
            votes_per_claim=1, concurrency=2, leaf_timeout=150, reason_timeout=90,
        )
        return await _real_run(question, **kw)

    monkeypatch.setattr(rm_mod, "run_research", small_run)

    agent = await db.get_system_agent()
    session = await mgr.create_session(
        agent["id"], "Research", str(tmp_path), backend="codex"
    )
    row = await rm.start(
        session.id, "What is the latest stable version of Python 3? Cite python.org."
    )
    await asyncio.wait_for(rm._tasks[row["id"]], timeout=360)

    final = await db.get_research_job(row["id"])
    assert final["status"] == "completed", f"job did not complete: {final}"
    assert final["injection_status"] == "delivered"
    assert final["report_path"], "no report file was written"
    assert injected, "no report was injected into the session"
    body = injected[0]
    assert body.startswith(f"[deep-research:{row['id']}]")

    # Prove REAL research happened — not a degraded/empty run. The injected
    # prefix echoes the question (which contains "python"), so checking the
    # whole body for "python" would hollow-pass; instead require the actual
    # report file to be substantial and cite a real source. (Read the file
    # rather than the question-bearing injection prefix.)
    with open(final["report_path"], encoding="utf-8") as fh:
        report = fh.read()
    assert len(report) > 120, f"report too short — likely a degraded run:\n{report}"
    assert "http" in report.lower(), f"report cites no source:\n{report}"

    await db.close()
