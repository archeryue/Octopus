"""Shared real-CLI availability gates for the `tests/*_real.py` suites.

Real-LLM tests must skip not just when a backend's binary is absent, but also
when it's present yet NOT signed in — otherwise an expired/lapsed login turns
the whole suite red with confusing downstream errors ("one-shot exited 1",
AI-parse failures) that look like product regressions but are really just a
logged-out CLI. (That's the exact failure harness-credential-reauth.md exists
to surface in the app.)

These probe ACTUAL usability once per session (lru_cache):
  - claude: a tiny real `--print` call must exit 0 (there's no auth.json to stat).
  - codex:  binary present AND ~/.codex/auth.json exists (a real call is slower;
            the login file is the same signal codex itself uses).

Imported as `from tests.cli_gate import claude_cli_works, codex_cli_works`
(the repo root is on sys.path during the test run, so `tests` resolves as a
namespace package — `import conftest` is NOT reliable under pytest).
"""

from __future__ import annotations

import functools
import os
import shutil
import subprocess


def _resolve_cli(binary: str) -> str | None:
    """Resolve a CLI honoring the same PATH fallback the harness uses (nvm /
    ~/.local/bin), so the gate matches how the backend actually launches it."""
    try:
        from server.harness.run import _which_with_fallback

        return _which_with_fallback(binary)
    except Exception:
        return shutil.which(binary)


@functools.lru_cache(maxsize=1)
def claude_cli_works() -> bool:
    """True only if `claude` is installed AND authenticated. Probes once with a
    minimal real call; a logged-out CLI exits non-zero (the 401), so dependent
    tests skip rather than fail."""
    exe = _resolve_cli("claude")
    if exe is None:
        return False
    try:
        proc = subprocess.run(
            [exe, "--print", "--", "ok"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


@functools.lru_cache(maxsize=1)
def codex_cli_works() -> bool:
    """True only if `codex` is installed AND actually authenticated. A present
    `~/.codex/auth.json` is NOT sufficient — its token can be invalidated while
    the file lingers (a real 401 we hit in practice). So we probe with a tiny
    real `codex exec`, exactly like the claude gate; a logged-out CLI exits
    non-zero, so dependent tests skip rather than hollow-pass/fail."""
    exe = _resolve_cli("codex")
    if exe is None:
        return False
    import tempfile

    try:
        proc = subprocess.run(
            [
                exe, "exec", "--json", "--skip-git-repo-check",
                "--dangerously-bypass-approvals-and-sandbox", "--", "Reply with OK.",
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            cwd=tempfile.gettempdir(),
            timeout=90,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    # A 401 / invalidated token exits non-zero and prints the auth error.
    return proc.returncode == 0
