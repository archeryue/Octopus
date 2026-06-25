from __future__ import annotations

import pytest

from server.showme_ai import (
    _bare_path_fallback,
    _format_messages,
    extract_json,
    resolve_local_path,
    resolve_showme_reference,
)


class FakeHarness:
    def __init__(self, out: str):
        self.out = out
        self.calls = []

    async def run_oneshot(self, ctx):
        self.calls.append(ctx)
        return self.out


async def _run(harness: FakeHarness, text: str = "this file", messages=None, **kwargs):
    return await resolve_showme_reference(
        text,
        harness=harness,
        model="m",
        credential=None,
        working_dir=kwargs.pop("working_dir", "/tmp/wd"),
        messages=messages if messages is not None else [],
        session_name=kwargs.pop("session_name", "Demo"),
        **kwargs,
    )


# --- Layer 1: exact-path short-circuit ---


def test_resolve_local_path_returns_existing_file(tmp_path):
    (tmp_path / "README.md").write_text("hi")
    assert resolve_local_path("README.md", str(tmp_path)) == "README.md"


def test_resolve_local_path_returns_none_for_missing(tmp_path):
    assert resolve_local_path("does-not-exist.md", str(tmp_path)) is None


def test_resolve_local_path_rejects_escape(tmp_path):
    (tmp_path.parent / "outside.txt").write_text("nope")
    assert resolve_local_path("../outside.txt", str(tmp_path)) is None


def test_resolve_local_path_handles_nested(tmp_path):
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "note.md").write_text("hi")
    assert resolve_local_path("sub/note.md", str(tmp_path)) == "sub/note.md"


def test_resolve_local_path_returns_none_for_empty_and_multiline(tmp_path):
    assert resolve_local_path("", str(tmp_path)) is None
    assert resolve_local_path("  ", str(tmp_path)) is None
    assert resolve_local_path("a\nb", str(tmp_path)) is None


@pytest.mark.asyncio
async def test_exact_path_short_circuits_no_model_call(tmp_path):
    (tmp_path / "README.md").write_text("hi")
    harness = FakeHarness('{"path":"unrelated.md"}')
    result = await _run(harness, text="README.md", working_dir=str(tmp_path))
    assert result.path == "README.md"
    # Critically: the model was NOT called.
    assert harness.calls == []


# --- Layer 2: robust JSON extraction ---


def test_extract_json_bare_object():
    assert extract_json('{"path":"x.md"}') == {"path": "x.md"}


def test_extract_json_strips_markdown_fence_with_lang_tag():
    assert extract_json('```json\n{"path":"x.md"}\n```') == {"path": "x.md"}


def test_extract_json_strips_bare_markdown_fence():
    assert extract_json('```\n{"path":"x.md"}\n```') == {"path": "x.md"}


def test_extract_json_tolerates_surrounding_prose():
    # The defining real-CLI failure mode: Claude adds context before/after.
    raw = 'Looking at the conversation, the file is:\n\n{"path":"README.md"}\n\nHope that helps!'
    assert extract_json(raw) == {"path": "README.md"}


def test_extract_json_returns_none_for_no_object():
    assert extract_json("just prose, no json here at all") is None
    assert extract_json("") is None
    assert extract_json("{ not valid json") is None


@pytest.mark.asyncio
async def test_json_object_in_prose_resolves(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "plan.md").write_text("the plan")
    harness = FakeHarness(
        'Looking at the conversation:\n\n{"path":"docs/plan.md"}\n\nDone.'
    )
    result = await _run(
        harness,
        working_dir=str(tmp_path),
        messages=[{"role": "user", "type": "text", "content": "open the plan"}],
    )
    assert result.path == "docs/plan.md"


@pytest.mark.asyncio
async def test_clarification_message_passes_through():
    harness = FakeHarness('{"message":"Which file do you mean?"}')
    result = await _run(harness, session_name=None)
    assert result.path is None
    assert result.message == "Which file do you mean?"


# --- Layer 3: bare-path fallback ---


def test_bare_path_fallback_accepts_filename():
    assert _bare_path_fallback("README.md") == "README.md"


def test_bare_path_fallback_accepts_relative_path():
    assert _bare_path_fallback("docs/plan.md") == "docs/plan.md"


def test_bare_path_fallback_strips_trailing_lines():
    # Model says the path then adds a confirmation sentence.
    out = "README.md\nThat's the readme file."
    assert _bare_path_fallback(out) == "README.md"


def test_bare_path_fallback_rejects_prose():
    assert _bare_path_fallback("The README is at README.md") is None
    assert _bare_path_fallback("I'm not sure which file") is None


def test_bare_path_fallback_rejects_without_extension():
    # "README" alone is too ambiguous — could be a misread token.
    assert _bare_path_fallback("README") is None


@pytest.mark.asyncio
async def test_bare_token_response_resolves_as_path(tmp_path):
    # `claude --print` very commonly replies with just the path token.
    (tmp_path / "README.md").write_text("hi")
    harness = FakeHarness("README.md")
    result = await _run(harness, working_dir=str(tmp_path))
    assert result.path == "README.md"


@pytest.mark.asyncio
async def test_json_path_nonexistent_returns_message(tmp_path):
    # Model confidently returns a path that doesn't exist on disk.
    harness = FakeHarness('{"path":"app/ideas.md"}')
    result = await _run(harness, working_dir=str(tmp_path))
    assert result.path is None
    assert "app/ideas.md" in (result.message or "")


@pytest.mark.asyncio
async def test_bare_fallback_nonexistent_returns_message(tmp_path):
    # Model replies with just a bare path token, but the file doesn't exist.
    harness = FakeHarness("src/missing.ts")
    result = await _run(harness, working_dir=str(tmp_path))
    assert result.path is None
    assert "src/missing.ts" in (result.message or "")


# --- Unrecoverable response ---


@pytest.mark.asyncio
async def test_unrecoverable_response_returns_friendly_message():
    harness = FakeHarness("I'm not sure which file you mean — try giving the path.")
    result = await _run(harness)
    assert result.path is None
    assert result.message  # generic fallback


# --- _format_messages helper ---


def test_format_messages_includes_tool_use_with_path():
    out = _format_messages(
        [
            {"role": "user", "type": "text", "content": "show me the plan"},
            {
                "role": "assistant",
                "type": "tool_use",
                "tool_name": "Read",
                "tool_input": {"path": "docs/plan.md"},
            },
        ]
    )
    assert "User: show me the plan" in out
    assert "Read" in out
    assert "docs/plan.md" in out


def test_format_messages_skips_tool_use_without_path():
    out = _format_messages(
        [{"role": "assistant", "type": "tool_use", "tool_name": "Bash", "tool_input": {"command": "ls"}}]
    )
    assert out == ""
