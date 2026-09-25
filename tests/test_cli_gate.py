"""The real-CLI gates decide whether the `*_real.py` suites run at all, so a
gate that answers wrongly is worse than a failing test: it turns 18 real tests
into skips and the suite still reads green.

CLAUDE.md's rule is that a skip means a lapsed login, not a passing suite.
These tests pin the distinction the gate has to draw — absent/logged-out is a
skip, "we couldn't tell" is not.

The second half of the file pins the *marker* invariants. The gates are
applied by markers (`real`, `real_claude`, `real_codex`, `claude_bin`,
`codex_bin`) resolved in `conftest.pytest_runtest_setup`, and that scheme has
the same silent-failure mode as a wrong gate: a `*_real.py` file that forgets
`pytest.mark.real` lands its live-model tests in the hermetic tier, where
`-m "not real"` will happily run them against a real CLI and nobody notices
until CI-less local runs start making network calls.
"""

import pathlib
import re
import subprocess

import pytest

from tests.cli_gate import CliProbeTimeout, _probe

_REAL_SUITES = sorted(pathlib.Path(__file__).parent.glob("test_*_real.py"))


def test_probe_returns_true_when_the_cli_answers():
    assert _probe(["true"], timeout=10) is True


def test_probe_returns_false_when_the_cli_fails():
    """A logged-out CLI exits non-zero — that IS evidence, so dependent tests
    legitimately skip."""
    assert _probe(["false"], timeout=10) is False


def test_probe_returns_false_when_the_binary_is_missing():
    assert _probe(["/nonexistent-binary-for-gate-test"], timeout=10) is False


def test_probe_retries_once_before_giving_up(monkeypatch):
    """A loaded box can push a trivial call past its limit; one retry absorbs
    that rather than declaring the CLI logged out."""
    calls: list[float] = []
    real_run = subprocess.run

    def flaky(argv, **kwargs):
        calls.append(kwargs["timeout"])
        if len(calls) == 1:
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        return real_run(["true"], **{k: v for k, v in kwargs.items() if k != "timeout"})

    monkeypatch.setattr(subprocess, "run", flaky)
    assert _probe(["true"], timeout=5) is True
    # Second attempt gets a longer budget than the first.
    assert calls == [5, 10]


def test_two_timeouts_raise_instead_of_skipping(monkeypatch):
    """The important one. Two timeouts mean we could not tell whether the CLI
    works — which must never be reported as "logged out", because that silently
    hollows out the suite."""
    def always_timeout(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", always_timeout)
    with pytest.raises(CliProbeTimeout, match="NOT a lapsed login"):
        _probe(["whatever"], timeout=1)


# --------------------------------------------------------------- marker invariants


def test_every_real_suite_is_discovered():
    """Guard the guard: if this glob ever returns nothing the two tests below
    pass vacuously and stop protecting anything."""
    assert len(_REAL_SUITES) >= 13, [p.name for p in _REAL_SUITES]


@pytest.mark.parametrize("path", _REAL_SUITES, ids=lambda p: p.name)
def test_real_suite_carries_the_real_marker(path):
    """Every `*_real.py` must opt into the `real` umbrella at module level.

    `-m "not real"` is the hermetic tier, so a file without this marker is not
    merely mis-labelled — its live-model tests join the tier that is supposed
    to be safe to run anywhere.
    """
    src = path.read_text()
    match = re.search(r"^pytestmark\s*=\s*(.+?)(?=\n\n|\n@|\nclass |\ndef )",
                      src, re.MULTILINE | re.DOTALL)
    assert match, f"{path.name} has no module-level pytestmark"
    assert "mark.real" in match.group(1), (
        f"{path.name} has a pytestmark but not pytest.mark.real: {match.group(1)!r}"
    )


@pytest.mark.parametrize("path", _REAL_SUITES, ids=lambda p: p.name)
def test_real_suite_does_not_probe_at_import(path):
    """No `*_real.py` may call a gate probe at module scope.

    `claude_cli_works()` shells out to a real `claude --print`, so evaluating
    it in a module-level `pytestmark = pytest.mark.skipif(...)` or a
    `HAS_CLAUDE = ...` constant made plain `pytest --collect-only` invoke the
    model — 16.44 s against 0.80 s, for a command that is meant to be free.
    The markers exist precisely to move that probe into setup; this test stops
    the old pattern coming back.
    """
    src = path.read_text()
    offenders = [
        line.strip()
        for line in src.splitlines()
        if re.search(r"\b(claude|codex)_cli_works\s*\(", line)
    ]
    assert not offenders, (
        f"{path.name} evaluates a gate probe at import; use a marker instead: "
        f"{offenders}"
    )
