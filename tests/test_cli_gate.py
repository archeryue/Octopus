"""The real-CLI gates decide whether the `*_real.py` suites run at all, so a
gate that answers wrongly is worse than a failing test: it turns 18 real tests
into skips and the suite still reads green.

CLAUDE.md's rule is that a skip means a lapsed login, not a passing suite.
These tests pin the distinction the gate has to draw — absent/logged-out is a
skip, "we couldn't tell" is not.
"""

import subprocess

import pytest

from tests.cli_gate import CliProbeTimeout, _probe


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
