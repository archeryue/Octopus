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
