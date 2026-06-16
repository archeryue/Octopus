"""Gated real-CLI tests for the full-copy fork strategy (session-fork-copy.md).

Each asserts the END-TO-END truth the unit tests can't: that the real CLI
actually RESUMES a transcript the harness copied to a new id/location and recalls
context from it. This is the exact check that de-risked the design (the manual
probe).

These call the real backend, so they're inherently subject to the environment:
the login can be expired/revoked and the provider can rate-limit/overload —
NONE of which is a product bug. So a CLI call that fails for an environmental
reason SKIPS the test (the cli_gate.py philosophy); only a usable CLI that
fails to recall the copied context FAILS. Auto-skips entirely unless the backend
CLI is installed and a signed-in home is found.
"""

import glob
import json
import os
import subprocess
import uuid

import pytest

from server.harness.run import _which_with_fallback
from server.harness import claude_code as cc
from server.harness import codex as cx
from server.harness.events import HarnessCredential

# Substrings that mark an ENVIRONMENTAL CLI failure (auth/quota/provider), not a
# fork-logic bug — when a non-zero call shows one, we skip instead of fail.
_ENV_FAIL_MARKERS = (
    "401", "unauthorized", "revoked", "token_invalidated", "invalidated",
    "sign in again", "log out", "session has ended",
    "rate limit", "rate limited", "temporarily limiting requests",
    "overloaded", "529", "500 internal", "503", "quota",
)


def _skip_if_env_failure(proc: subprocess.CompletedProcess, what: str) -> None:
    """Skip (not fail) when a non-zero CLI call looks environmental — a dead
    login or a provider throttle, not a fork-copy defect."""
    if proc.returncode == 0:
        return
    blob = ((proc.stdout or "") + "\n" + (proc.stderr or "")).lower()
    if any(m in blob for m in _ENV_FAIL_MARKERS):
        pytest.skip(f"{what}: environmental CLI failure (auth/rate-limit)")


@pytest.mark.skipif(_which_with_fallback("claude") is None, reason="claude CLI not installed")
@pytest.mark.asyncio
async def test_claude_native_copy_resume_recalls_context(tmp_path):
    exe = _which_with_fallback("claude")
    src = tmp_path / "src"; src.mkdir()
    dst = tmp_path / "dst"; dst.mkdir()

    r = subprocess.run(
        [exe, "--print", "--output-format=json", "--dangerously-skip-permissions",
         "Remember this exactly: the codeword is ZEBRA77. Acknowledge in 3 words."],
        cwd=str(src), capture_output=True, text=True, timeout=120,
    )
    _skip_if_env_failure(r, "claude create")
    assert r.returncode == 0, r.stderr
    sid = json.loads(r.stdout)["session_id"]

    nid = str(uuid.uuid4())
    art = await cc._fork_copy(
        parent_working_dir=str(src), parent_resume_id=sid,
        dest_working_dir=str(dst), new_resume_id=nid,
    )
    assert art.needs_replay is False and art.resume_id == nid

    try:
        r2 = subprocess.run(
            [exe, "--print", "--output-format=json", "--dangerously-skip-permissions",
             "--resume", nid,
             "What is the codeword I told you earlier? Reply with ONLY the word."],
            cwd=str(dst), capture_output=True, text=True, timeout=120,
        )
        _skip_if_env_failure(r2, "claude resume")
        assert r2.returncode == 0, r2.stderr
        # The real assertion: the COPIED transcript carried the context.
        assert "ZEBRA77" in json.loads(r2.stdout).get("result", "")
    finally:
        import shutil
        for wd in (str(src), str(dst)):
            shutil.rmtree(cc._claude_project_dir(wd), ignore_errors=True)


def _codex_home_with_auth() -> str | None:
    """A signed-in CODEX_HOME: the host default if it has auth.json, else any
    Octopus per-credential dir (~/.octopus/codex/<id>/auth.json). None if none.
    (Presence only — a revoked token still passes here but the env-failure skip
    below catches it at call time.)"""
    host = os.path.expanduser("~/.codex")
    if os.path.exists(os.path.join(host, "auth.json")):
        return host
    for p in glob.glob(os.path.expanduser("~/.octopus/codex/*/auth.json")):
        return os.path.dirname(p)
    return None


@pytest.mark.skipif(_which_with_fallback("codex") is None, reason="codex CLI not installed")
@pytest.mark.asyncio
async def test_codex_native_copy_resume_recalls_context(tmp_path):
    exe = _which_with_fallback("codex")
    codex_home = _codex_home_with_auth()
    if codex_home is None:
        pytest.skip("no signed-in CODEX_HOME found")
    src = tmp_path / "src"; src.mkdir()
    dst = tmp_path / "dst"; dst.mkdir()
    cred = HarnessCredential(backend="codex", auth_type="oauth", home_dir=codex_home)
    env = {**os.environ, "CODEX_HOME": codex_home}

    def _run(wd, extra):
        return subprocess.run(
            [exe, "exec", "--json", "--skip-git-repo-check", "-C", str(wd),
             "--dangerously-bypass-approvals-and-sandbox", *extra],
            capture_output=True, text=True, timeout=180, env=env,
        )

    r = _run(src, ["--", "Remember this exactly: the codeword is ZEBRA77. Acknowledge in 3 words."])
    _skip_if_env_failure(r, "codex create")
    assert r.returncode == 0, r.stderr
    rid = None
    for line in r.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(d, dict):
            rid = d.get("session_id") or d.get("thread_id") or (
                (d.get("payload") or {}).get("id") if isinstance(d.get("payload"), dict) else None
            )
        if rid:
            break
    assert rid, f"could not find rollout id in: {r.stdout[:500]}"

    nid = str(uuid.uuid4())
    art = await cx._fork_copy(
        parent_working_dir=str(src), parent_resume_id=rid,
        parent_credential=cred, dest_working_dir=str(dst), new_resume_id=nid,
    )
    assert art.needs_replay is False and art.resume_id == nid

    try:
        r2 = _run(dst, ["resume", nid, "--",
                        "What is the codeword I told you earlier? Reply with ONLY the word."])
        _skip_if_env_failure(r2, "codex resume")
        assert r2.returncode == 0, r2.stderr
        assert "ZEBRA77" in r2.stdout
    finally:
        copied = cx._find_rollout(cx._codex_sessions_dir(cred), nid)
        if copied is not None:
            copied.unlink(missing_ok=True)
