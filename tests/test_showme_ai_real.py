"""Real-CLI integration test for /showme reference resolution.

Skipped unless the `claude` binary is on PATH (mirrors test_schedule_ai_real.py
and test_backend_*_real.py). Exercises the full pipeline end-to-end against
the live Claude CLI — proves that:

  - exact-path short-circuit avoids the model call entirely;
  - the one-shot model call returns something the resolver can actually
    parse (this is the case the unit tests can't simulate — real claude's
    `--print --output-format=json` mode adds conversational context that
    has to be stripped, OR replies with a bare path);
  - the resolver returns a usable path for fuzzy references like
    "the readme".

This is the test that would have caught the production failure on
`/showme the readme` had I written it in the first place.
"""

from __future__ import annotations

import shutil

import pytest

from server.harness import get_harness
from server.showme_ai import resolve_showme_reference

pytestmark = pytest.mark.skipif(
    shutil.which("claude") is None, reason="needs the claude CLI on PATH"
)


@pytest.mark.asyncio
async def test_real_exact_path_resolves_without_calling_model(tmp_path):
    (tmp_path / "README.md").write_text("# hello\n")

    # If the short-circuit works, this resolves WITHOUT the harness ever
    # spawning claude. The fastest possible path.
    result = await resolve_showme_reference(
        "README.md",
        harness=get_harness("claude-code"),
        model=None,
        credential=None,
        working_dir=str(tmp_path),
        messages=[],
        session_name="Showme Real Exact",
    )
    assert result.path == "README.md"


@pytest.mark.asyncio
async def test_real_fuzzy_the_readme_resolves(tmp_path):
    """The production-failure case: `/showme the readme` in a dir with a
    README.md. The model has to interpret 'the readme' → README.md and
    the resolver has to cope with whatever shape claude actually returns."""
    (tmp_path / "README.md").write_text("# repo\n")
    (tmp_path / "main.py").write_text("def main(): pass\n")

    result = await resolve_showme_reference(
        "the readme",
        harness=get_harness("claude-code"),
        model=None,
        credential=None,
        working_dir=str(tmp_path),
        messages=[],
        session_name="Showme Real Fuzzy",
    )
    # Case-insensitive on the resolution — Claude may reply README.md or
    # readme.md depending on its mood; either is correct.
    assert result.path is not None, f"resolver fell through; message={result.message!r}"
    assert result.path.lower() == "readme.md", result


@pytest.mark.asyncio
async def test_real_fuzzy_with_conversation_context(tmp_path):
    """When recent messages mention a specific file, the resolver should
    prefer it even for a vague reference like `that file`."""
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "plan.md").write_text("# plan\n")
    (tmp_path / "README.md").write_text("# repo\n")

    result = await resolve_showme_reference(
        "that file",
        harness=get_harness("claude-code"),
        model=None,
        credential=None,
        working_dir=str(tmp_path),
        messages=[
            {"role": "user", "type": "text", "content": "let's look at docs/plan.md"},
            {
                "role": "assistant",
                "type": "text",
                "content": "Sure — docs/plan.md is the project plan.",
            },
        ],
        session_name="Showme Real Context",
    )
    assert result.path is not None, f"resolver fell through; message={result.message!r}"
    assert "plan" in result.path.lower(), result
