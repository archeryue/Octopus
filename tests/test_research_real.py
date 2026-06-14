"""Real end-to-end deep research against a signed-in harness CREDENTIAL.

Unlike the bare-CLI *_real tests (which use host ambient auth), deep research
runs through an Octopus credential. The codex credential is directory-backed
(its own CODEX_HOME with auth.json), so we discover a usable one on disk and
bind a session to its id — no DB credential row needed, since codex resolution
is purely path-based. Gated + skipped when no usable codex credential exists.
Costs real API calls + ~30-60s. Drives the WHOLE stack:
ResearchManager → orchestrator → real codex web_search → cited report → inject.
"""

from __future__ import annotations

import asyncio
import functools
import os
import subprocess
import tempfile

import pytest

from server.codex_login import codex_home_for
from server.database import Database
from server.research import manager as rm_mod
from server.research.manager import ResearchManager
from server.research.orchestrator import ResearchLimits, run_research as _real_run
from server.session_manager import SessionManager
from tests.cli_gate import _resolve_cli


@functools.lru_cache(maxsize=1)
def _usable_codex_credential() -> str | None:
    """A signed-in, directory-backed codex credential id whose CODEX_HOME holds
    a WORKING auth.json (probed for real). None if none is usable."""
    exe = _resolve_cli("codex")
    if exe is None:
        return None
    base = os.path.dirname(codex_home_for("_probe_"))  # the per-credential root
    if not os.path.isdir(base):
        return None
    for cid in sorted(os.listdir(base)):
        home = os.path.join(base, cid)
        if not os.path.exists(os.path.join(home, "auth.json")):
            continue
        env = {**os.environ, "CODEX_HOME": home}
        try:
            proc = subprocess.run(
                [exe, "exec", "--json", "--skip-git-repo-check",
                 "--dangerously-bypass-approvals-and-sandbox", "--", "Reply with OK."],
                stdin=subprocess.DEVNULL, capture_output=True,
                cwd=tempfile.gettempdir(), env=env, timeout=90,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if proc.returncode == 0:
            return cid
    return None


pytestmark = pytest.mark.skipif(
    _usable_codex_credential() is None,
    reason="no signed-in codex credential available for a real research run",
)


@pytest.mark.asyncio
async def test_real_codex_deep_research_end_to_end(tmp_path, monkeypatch):
    cred_id = _usable_codex_credential()
    db = Database(":memory:")
    await db.initialize()
    mgr = SessionManager()
    await mgr.initialize(db)
    rm = ResearchManager()
    rm.bind(session_mgr=mgr, db=db)

    injected: list[str] = []

    async def fake_start_message(session_id, prompt):
        injected.append(prompt)

    monkeypatch.setattr(mgr, "start_message", fake_start_message)

    # Shrink limits hard for speed — still REAL web searches.
    async def small_run(question, **kw):
        kw["limits"] = ResearchLimits(
            max_angles=1, max_findings_per_angle=3, max_claims=2,
            votes_per_claim=1, concurrency=2, leaf_timeout=180, reason_timeout=120,
        )
        return await _real_run(question, **kw)

    monkeypatch.setattr(rm_mod, "run_research", small_run)

    agent = await db.get_system_agent()
    # Bind the session to the directory-backed codex credential id — the
    # manager resolves it to its CODEX_HOME (no DB row needed).
    session = await mgr.create_session(
        agent["id"], "Research", str(tmp_path), backend="codex",
        credential_id=cred_id,
    )
    row = await rm.start(
        session.id,
        "What is the latest stable version of Python 3? Cite python.org.",
    )
    await asyncio.wait_for(rm._tasks[row["id"]], timeout=360)

    final = await db.get_research_job(row["id"])
    assert final["status"] == "completed", f"job did not complete: {final}"
    assert final["injection_status"] == "delivered"
    assert final["report_path"], "no report file written"

    # Prove REAL research happened (not a degraded empty run): a substantial,
    # source-citing report read from the FILE (not the question-bearing
    # injection prefix, which would hollow-pass on the echoed question).
    with open(final["report_path"], encoding="utf-8") as fh:
        report = fh.read()
    assert len(report) > 120, f"report too short — degraded run?\n{report}"
    assert "http" in report.lower(), f"report cites no source:\n{report}"

    await db.close()
