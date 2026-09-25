"""Global test configuration — ensure tests use known default settings."""

import atexit
import os
import shutil
import tempfile

# Override env vars before any module imports Settings, so tests
# don't pick up values from the user's .env file.
os.environ["OCTOPUS_AUTH_TOKEN"] = "changeme"

# Isolate per-agent state (the canonical memory dir each agent gets) under a
# throwaway temp root, so creating agents in tests never litters the
# developer's real ~/.octopus/agents (docs/plans/memory.md). Set before any
# import of Settings; cleaned at process exit. Per-test fixtures may still
# point agents_dir at their own tmp_path — that just overrides this default.
_TEST_AGENTS_DIR = tempfile.mkdtemp(prefix="octopus-test-agents-")
os.environ["OCTOPUS_AGENTS_DIR"] = _TEST_AGENTS_DIR
atexit.register(lambda: shutil.rmtree(_TEST_AGENTS_DIR, ignore_errors=True))

# Real-CLI availability gates live in tests/cli_gate.py (imported by the
# *_real.py suites as `from tests.cli_gate import …`); they're not here because
# `import conftest` isn't reliably resolvable under pytest collection.


def pytest_runtest_setup(item):
    """Apply the real-CLI markers — at setup, deliberately not at import.

    The `*_real.py` suites used to compute their skip condition at module
    level (`pytestmark = pytest.mark.skipif(not claude_cli_works(), …)`, or
    a `HAS_CLAUDE = claude_cli_works()` constant). Because `claude_cli_works`
    probes by making a real `claude --print` call, that turned plain
    collection into something that invoked the model: `--collect-only` took
    16.44 s against 0.80 s for the hermetic tier, and `pytest --collect-only`
    is supposed to be free.

    Running the probe here instead means it fires only when a test carrying
    the marker is genuinely about to run — and never at all under
    `-m "not real"`, which deselects before setup.

    `cli_gate` is imported inside the function for the same reason: keep
    importing conftest side-effect-free. Both probes are `lru_cache`d there,
    so the cost is paid at most once per CLI per session no matter how many
    marked tests run.
    """
    from tests.cli_gate import claude_cli_works, codex_cli_works
    from server.harness.run import _which_with_fallback

    checks = (
        # marker         predicate                                    skip reason
        ("real_claude", claude_cli_works, "claude CLI unavailable or not signed in"),
        ("real_codex", codex_cli_works, "codex CLI unavailable or not signed in"),
        ("claude_bin", lambda: _which_with_fallback("claude") is not None,
         "claude CLI not installed"),
        ("codex_bin", lambda: _which_with_fallback("codex") is not None,
         "codex CLI not installed"),
    )
    for marker, is_available, reason in checks:
        if item.get_closest_marker(marker) and not is_available():
            import pytest

            pytest.skip(reason)
