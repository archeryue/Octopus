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


class CliProbeTimeout(RuntimeError):
    """A gate probe timed out, twice.

    Deliberately an error rather than a `False`. `False` means "this CLI is
    absent or logged out", which turns dependent tests into skips — and a skip
    that really meant "the box was busy" is a silently hollow suite, the exact
    thing these gates exist to prevent. A timeout is not evidence about the
    login; it's evidence we couldn't tell, so the suite says so out loud.
    """


def _probe(argv: list[str], *, timeout: float, cwd: str | None = None) -> bool:
    """Run a gate probe, retrying once with a doubled timeout.

    A loaded machine (a parallel suite, several CLIs mid-turn) can push a
    trivial call past its limit; one retry absorbs that. Two timeouts in a row
    is not load, and is reported rather than swallowed.
    """
    last: Exception | None = None
    for _attempt, limit in enumerate((timeout, timeout * 2)):
        try:
            proc = subprocess.run(
                argv, stdin=subprocess.DEVNULL, capture_output=True,
                timeout=limit, cwd=cwd,
            )
            return proc.returncode == 0
        except subprocess.TimeoutExpired as exc:
            last = exc
            continue
        except OSError:
            return False
    raise CliProbeTimeout(
        f"{argv[0]} did not answer a trivial probe within "
        f"{timeout:.0f}s or {timeout * 2:.0f}s. This is NOT a lapsed login — "
        f"tests must not be skipped on it. Re-run on a less loaded machine, "
        f"or fix the CLI. (last: {last})"
    )


@functools.lru_cache(maxsize=1)
def claude_cli_works() -> bool:
    """True only if `claude` is installed AND authenticated. Probes once with a
    minimal real call; a logged-out CLI exits non-zero (the 401), so dependent
    tests skip rather than fail."""
    exe = _resolve_cli("claude")
    if exe is None:
        return False
    return _probe([exe, "--print", "--", "ok"], timeout=60)


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

    # A 401 / invalidated token exits non-zero and prints the auth error.
    return _probe(
        [
            exe, "exec", "--json", "--skip-git-repo-check",
            "--dangerously-bypass-approvals-and-sandbox", "--", "Reply with OK.",
        ],
        timeout=90,
        cwd=tempfile.gettempdir(),
    )
