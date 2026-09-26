"""Global test configuration — ensure tests use known default settings."""

import atexit
import os
import shutil
import signal
import tempfile
import time
from pathlib import Path

import pytest

# Override env vars before any module imports Settings, so tests
# don't pick up values from the user's .env file.
os.environ["OCTOPUS_AUTH_TOKEN"] = "changeme"

_PROC = Path("/proc")

# Isolate per-agent state (the canonical memory dir each agent gets) under a
# throwaway temp root, so creating agents in tests never litters the
# developer's real ~/.octopus/agents (docs/plans/memory.md). Set before any
# import of Settings; cleaned at process exit. Per-test fixtures may still
# point agents_dir at their own tmp_path — that just overrides this default.
_TEST_AGENTS_DIR = tempfile.mkdtemp(prefix="octopus-test-agents-")
os.environ["OCTOPUS_AGENTS_DIR"] = _TEST_AGENTS_DIR
atexit.register(lambda: shutil.rmtree(_TEST_AGENTS_DIR, ignore_errors=True))

# A wider MCP startup budget for the real-CLI tier than production needs.
# Those tests run the callback server, the turn engine and two or three live
# CLIs inside one process and one event loop, and each CLI opens a handshake per
# namespace on startup. Measured in isolation a handshake takes ~0.1 s, but
# under that pressure the CLI's default budget is occasionally missed, and the
# CLI then reports the namespace as `CONNECT_TIMEOUT` and drops its tools — a
# test failure that says "the model ignored the instruction" when the tool was
# never offered. Production serves from its own process; this is test pressure,
# so it is corrected here rather than in the product.
os.environ.setdefault("MCP_TIMEOUT", "30000")

# Real-CLI availability gates live in tests/cli_gate.py (imported by the
# *_real.py suites as `from tests.cli_gate import …`); they're not here because
# `import conftest` isn't reliably resolvable under pytest collection.


def _ppid(pid: int) -> int | None:
    """Parent pid from /proc/<pid>/stat — same trick the monitor sampler uses.

    The comm field can contain spaces and parentheses, so everything up to the
    last ')' is skipped rather than split on.
    """
    try:
        raw = (_PROC / str(pid) / "stat").read_text()
    except OSError:
        return None
    try:
        return int(raw[raw.rindex(")") + 1 :].split()[1])
    except (ValueError, IndexError):
        return None


def _surviving_cli_pids() -> list[int]:
    """Live `claude` / `codex` processes descended from this pytest run.

    Ancestry is checked, not just the name: this box runs a production Octopus
    with CLIs of its own, and a test suite has no business killing those.
    """
    me = os.getpid()
    found: list[int] = []
    for entry in _PROC.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == me:
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes().decode("utf-8", "replace")
        except OSError:
            continue
        if "claude" not in cmdline and "codex" not in cmdline:
            continue
        cur: int | None = pid
        for _ in range(8):  # a CLI sits a couple of hops below us at most
            cur = _ppid(cur) if cur else None
            if cur is None or cur == 1:
                break
            if cur == me:
                found.append(pid)
                break
    return found


@pytest.fixture(autouse=True)
def _no_cli_left_behind(request):
    """Release every CLI a real test started, whether or not it remembered to.

    A held `claude` is ~250 MB and lives until its manager is told to stop.
    Several of the `*_real.py` suites build their own `SessionManager` and never
    tell it, so the tier accumulated idle CLIs as it ran — which is how a suite
    where every test passes alone starts failing in a group: the box gets slow
    enough that a CLI's own MCP handshake times out, and the model then reports
    the tool as unavailable. (inline-steering.md §7 records the OOM this caused
    the first time.)

    Hermetic tests spawn nothing, so this only runs for the `real` marker.
    """
    yield
    if request.node.get_closest_marker("real") is None:
        return
    survivors = _surviving_cli_pids()
    for pid in survivors:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    if not survivors:
        return
    time.sleep(0.3)
    for pid in _surviving_cli_pids():
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


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
    from server.harness.run import _which_with_fallback
    from tests.cli_gate import claude_cli_works, codex_cli_works

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


@pytest.fixture
def mcp_scope(monkeypatch):
    """Put a tool call in a session's scope, the way a real HTTP request does.

    The MCP namespaces are served in-process now rather than spawned per session
    (docs/plans/polish-2026-09.md §4 B1), so a tool's session no longer arrives
    as `OCTOPUS_SESSION_ID` in a process environment — one process serves every
    session, and the session comes from the verified scope of the request. Tests
    that used to `monkeypatch.setenv` establish a scope instead.

    `api_base` is redirected too, so a test can keep asserting against a stable
    fake host rather than whatever port `settings` happens to hold.
    """
    from server.mcp_identity import McpScope, reset_current_scope, set_current_scope
    from server.mcp_servers import _host

    state = {"session_id": "s1", "installation_id": None, "api_base": "http://x"}
    monkeypatch.setattr(_host, "api_base", lambda: state["api_base"])

    def _enter():
        return set_current_scope(
            McpScope(state["session_id"], state["installation_id"])
        )

    token = _enter()

    class Control:
        """Lets a test move the scope mid-test (e.g. to assert isolation)."""

        def set(self, session_id=None, installation_id=None, api_base=None):
            nonlocal token
            if session_id is not None:
                state["session_id"] = session_id
            if installation_id is not None:
                state["installation_id"] = installation_id
            if api_base is not None:
                state["api_base"] = api_base
            reset_current_scope(token)
            token = _enter()

        def clear(self):
            nonlocal token
            reset_current_scope(token)
            token = set_current_scope(None)

    yield Control()
    reset_current_scope(token)
