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
from server.research.orchestrator import ResearchLimits
from server.research.orchestrator import run_research as _real_run
from server.session_manager import SessionManager
from tests.cli_gate import _resolve_cli


def _codex_works_in(exe: str, home: str) -> bool:
    """A tiny real `codex exec` against `home` — the file can outlive its token."""
    if not os.path.exists(os.path.join(home, "auth.json")):
        return False
    try:
        proc = subprocess.run(
            [exe, "exec", "--json", "--skip-git-repo-check",
             "--dangerously-bypass-approvals-and-sandbox", "--", "Reply with OK."],
            stdin=subprocess.DEVNULL, capture_output=True,
            cwd=tempfile.gettempdir(), env={**os.environ, "CODEX_HOME": home},
            timeout=90,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


@functools.lru_cache(maxsize=1)
def _usable_codex_home() -> str | None:
    """A CODEX_HOME holding a WORKING sign-in (probed for real), or None.

    The box's own `~/.codex` — the login the CLI uses. Not a deployed install's
    per-credential directories: this suite used to find one by scanning the
    default `~/.octopus/codex`, which is a running install's state, and a test
    has no business signing in as somebody's live credential (or refreshing its
    token out from under them).
    """
    exe = _resolve_cli("codex")
    if exe is None:
        return None
    home = os.path.expanduser("~/.codex")
    return home if _codex_works_in(exe, home) else None


_TEST_CREDENTIAL = "codex-test-host"


def _usable_codex_credential() -> str | None:
    """A directory-backed codex credential id for this test's isolated
    `codex_home_dir`, or None if the box has no working codex sign-in.

    A codex credential *is* its directory (`codex_home_for(id)`), so the
    credential is a symlink to the working home rather than a copy: a copy
    would be a second holder of the same refresh token, and the first refresh
    through either would invalidate the other. Not cached — `codex_home_dir`
    is a fresh tmp root per test (tests/conftest.py), so the link is made
    wherever this test's root is.
    """
    home = _usable_codex_home()
    if home is None:
        return None
    link = codex_home_for(_TEST_CREDENTIAL)
    os.makedirs(os.path.dirname(link), exist_ok=True)
    if not os.path.lexists(link):
        os.symlink(home, link)
    return _TEST_CREDENTIAL


pytestmark = [pytest.mark.real, pytest.mark.real_codex]


@pytest.fixture(autouse=True)
def _require_signed_in_codex_credential():
    """This suite needs more than a signed-in CLI: it needs a usable codex
    credential resolvable from the DB. That probe is file-local (it shells
    out), so it cannot be a conftest marker — but it must still not run at
    import time. An autouse fixture runs at setup, which is the same
    deferral the markers get.
    """
    if _usable_codex_credential() is None:
        pytest.skip("no signed-in codex credential available for a real research run")


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
