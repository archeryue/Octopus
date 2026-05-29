from __future__ import annotations

import pytest

from server.showme_ai import _format_messages, resolve_showme_reference


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
        working_dir="/tmp/wd",
        messages=messages if messages is not None else [],
        session_name=kwargs.pop("session_name", "Demo"),
        **kwargs,
    )


@pytest.mark.asyncio
async def test_returns_path_on_clean_json():
    harness = FakeHarness('{"path":"docs/plan.md"}')
    result = await _run(
        harness,
        messages=[{"role": "user", "type": "text", "content": "open docs/plan.md"}],
    )
    assert result.path == "docs/plan.md"
    assert result.message is None
    assert harness.calls[0].working_dir == "/tmp/wd"
    assert "/showme this file" in harness.calls[0].prompt


@pytest.mark.asyncio
async def test_returns_message_when_model_asks_to_clarify():
    harness = FakeHarness('{"message":"Which file do you mean?"}')
    result = await _run(harness, session_name=None)
    assert result.path is None
    assert result.message == "Which file do you mean?"


@pytest.mark.asyncio
async def test_strips_markdown_code_fence_with_json_tag():
    # The model is told NOT to fence, but defensive parsing keeps this from
    # rotting silently on a model that ignores the instruction.
    harness = FakeHarness('```json\n{"path":"README.md"}\n```')
    result = await _run(harness)
    assert result.path == "README.md"


@pytest.mark.asyncio
async def test_strips_bare_code_fence_without_lang_tag():
    harness = FakeHarness('```\n{"path":"a/b.py"}\n```')
    result = await _run(harness)
    assert result.path == "a/b.py"


@pytest.mark.asyncio
async def test_malformed_json_returns_generic_message():
    harness = FakeHarness("not json at all")
    result = await _run(harness)
    assert result.path is None
    assert result.message and "unexpected" in result.message.lower()


@pytest.mark.asyncio
async def test_empty_path_falls_through_to_message():
    # `{"path": ""}` should not be treated as resolved.
    harness = FakeHarness('{"path":""}')
    result = await _run(harness)
    assert result.path is None
    assert result.message  # generic "couldn't resolve" fallback


@pytest.mark.asyncio
async def test_both_path_and_message_path_wins():
    harness = FakeHarness('{"path":"x.md","message":"also here is a clarification"}')
    result = await _run(harness)
    assert result.path == "x.md"
    assert result.message is None


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
    # Tool calls that don't carry a `path` aren't formatted (no signal).
    out = _format_messages(
        [{"role": "assistant", "type": "tool_use", "tool_name": "Bash", "tool_input": {"command": "ls"}}]
    )
    assert out == ""
