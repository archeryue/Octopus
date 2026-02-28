"""Tests for the JSONL parser that converts Claude Code sessions to Octopus format."""

import json
import tempfile
from pathlib import Path

import pytest

from server.jsonl_parser import (
    ParsedSession,
    SessionMetadata,
    _consolidate_messages,
    _convert_line,
    _extract_session_metadata,
    _find_primary_session_id,
    parse_jsonl_file,
    parse_jsonl_lines,
)
from server.models import MessageContent
from server.models import MessageRole


def _make_line(type_: str, role: str, content, session_id: str = "sess-1", **extra):
    data = {
        "type": type_,
        "sessionId": session_id,
        "cwd": "/home/user/project",
        "timestamp": "2026-01-01T00:00:00Z",
        "message": {"role": role, "content": content},
        **extra,
    }
    return json.dumps(data)


class TestConvertLine:
    def test_user_text_string(self):
        data = json.loads(_make_line("user", "user", "hello"))
        result = _convert_line(data)
        assert result is not None
        assert len(result) == 1
        assert result[0].role == MessageRole.user
        assert result[0].type == "text"
        assert result[0].content == "hello"

    def test_user_text_block(self):
        data = json.loads(
            _make_line("user", "user", [{"type": "text", "text": "hello block"}])
        )
        result = _convert_line(data)
        assert result is not None
        assert len(result) == 1
        assert result[0].role == MessageRole.user
        assert result[0].content == "hello block"

    def test_assistant_text_block(self):
        data = json.loads(
            _make_line(
                "assistant", "assistant", [{"type": "text", "text": "I can help"}]
            )
        )
        result = _convert_line(data)
        assert result is not None
        assert len(result) == 1
        assert result[0].role == MessageRole.assistant
        assert result[0].type == "text"
        assert result[0].content == "I can help"

    def test_assistant_tool_use(self):
        data = json.loads(
            _make_line(
                "assistant",
                "assistant",
                [
                    {
                        "type": "tool_use",
                        "name": "Read",
                        "input": {"file_path": "/foo.py"},
                        "id": "toolu_123",
                    }
                ],
            )
        )
        result = _convert_line(data)
        assert result is not None
        assert len(result) == 1
        assert result[0].role == MessageRole.assistant
        assert result[0].type == "tool_use"
        assert result[0].tool_name == "Read"
        assert result[0].tool_input == {"file_path": "/foo.py"}
        assert result[0].tool_use_id == "toolu_123"

    def test_assistant_mixed_blocks(self):
        data = json.loads(
            _make_line(
                "assistant",
                "assistant",
                [
                    {"type": "text", "text": "Let me read that."},
                    {
                        "type": "tool_use",
                        "name": "Read",
                        "input": {"file_path": "/f.py"},
                        "id": "toolu_456",
                    },
                ],
            )
        )
        result = _convert_line(data)
        assert result is not None
        assert len(result) == 2
        assert result[0].type == "text"
        assert result[1].type == "tool_use"

    def test_tool_result(self):
        data = json.loads(
            _make_line(
                "user",
                "user",
                [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_123",
                        "content": "file contents here",
                    }
                ],
            )
        )
        result = _convert_line(data)
        assert result is not None
        assert len(result) == 1
        assert result[0].role == MessageRole.tool
        assert result[0].type == "tool_result"
        assert result[0].tool_use_id == "toolu_123"
        assert result[0].content == "file contents here"
        assert result[0].is_error is None

    def test_tool_result_with_error(self):
        data = json.loads(
            _make_line(
                "user",
                "user",
                [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_err",
                        "content": "Permission denied",
                        "is_error": True,
                    }
                ],
            )
        )
        result = _convert_line(data)
        assert result is not None
        assert result[0].is_error is True

    def test_skip_queue_operation(self):
        data = {"type": "queue-operation", "data": {}}
        assert _convert_line(data) is None

    def test_skip_unknown_type(self):
        data = {"type": "file-history-snapshot", "snapshot": {}}
        assert _convert_line(data) is None

    def test_skip_progress(self):
        data = {"type": "progress", "sessionId": "abc"}
        assert _convert_line(data) is None

    def test_assistant_thinking_skipped(self):
        """Thinking blocks should be skipped (not text or tool_use)."""
        data = json.loads(
            _make_line(
                "assistant",
                "assistant",
                [{"type": "thinking", "thinking": "hmm..."}],
            )
        )
        result = _convert_line(data)
        assert result is None


class TestExtractMetadata:
    def test_basic_metadata(self):
        lines = [
            json.loads(_make_line("user", "user", "hello world", session_id="sid-1"))
        ]
        meta = _extract_session_metadata(lines)
        assert meta.session_id == "sid-1"
        assert meta.cwd == "/home/user/project"
        assert meta.first_user_message == "hello world"
        assert meta.timestamp == "2026-01-01T00:00:00Z"

    def test_skips_non_message_lines(self):
        lines = [
            {"type": "file-history-snapshot", "snapshot": {}},
            json.loads(_make_line("user", "user", "first msg", session_id="s1")),
        ]
        meta = _extract_session_metadata(lines)
        assert meta.session_id == "s1"
        assert meta.first_user_message == "first msg"


class TestFindPrimarySessionId:
    def test_uses_hint_when_present(self):
        lines = [
            json.loads(_make_line("user", "user", "stray", session_id="other")),
            json.loads(_make_line("user", "user", "main1", session_id="target")),
            json.loads(_make_line("assistant", "assistant", [{"type": "text", "text": "r"}], session_id="target")),
        ]
        assert _find_primary_session_id(lines, hint="target") == "target"

    def test_falls_back_to_most_common(self):
        lines = [
            json.loads(_make_line("user", "user", "stray", session_id="rare")),
            json.loads(_make_line("user", "user", "a", session_id="common")),
            json.loads(_make_line("assistant", "assistant", [{"type": "text", "text": "b"}], session_id="common")),
            json.loads(_make_line("assistant", "assistant", [{"type": "text", "text": "c"}], session_id="common")),
        ]
        assert _find_primary_session_id(lines, hint=None) == "common"

    def test_hint_not_in_file_uses_most_common(self):
        lines = [
            json.loads(_make_line("user", "user", "a", session_id="actual")),
            json.loads(_make_line("assistant", "assistant", [{"type": "text", "text": "b"}], session_id="actual")),
        ]
        assert _find_primary_session_id(lines, hint="nonexistent") == "actual"


class TestConsolidateMessages:
    def test_merges_consecutive_assistant_text(self):
        msgs = [
            MessageContent(role=MessageRole.assistant, type="text", content="Part 1"),
            MessageContent(role=MessageRole.assistant, type="text", content="Part 2"),
        ]
        result = _consolidate_messages(msgs)
        assert len(result) == 1
        assert result[0].content == "Part 1\n\nPart 2"

    def test_merges_consecutive_user_text(self):
        msgs = [
            MessageContent(role=MessageRole.user, type="text", content="Q1"),
            MessageContent(role=MessageRole.user, type="text", content="Q2"),
        ]
        result = _consolidate_messages(msgs)
        assert len(result) == 1
        assert "Q1" in result[0].content
        assert "Q2" in result[0].content

    def test_does_not_merge_different_roles(self):
        msgs = [
            MessageContent(role=MessageRole.user, type="text", content="Q"),
            MessageContent(role=MessageRole.assistant, type="text", content="A"),
        ]
        result = _consolidate_messages(msgs)
        assert len(result) == 2

    def test_folds_tool_result_into_tool_use(self):
        msgs = [
            MessageContent(
                role=MessageRole.assistant, type="tool_use",
                tool_name="Read", tool_input={"path": "/f"}, tool_use_id="t1",
            ),
            MessageContent(
                role=MessageRole.tool, type="tool_result",
                content="file data here", tool_use_id="t1",
            ),
        ]
        result = _consolidate_messages(msgs)
        assert len(result) == 1
        assert result[0].type == "tool_use"
        assert result[0].tool_name == "Read"
        assert result[0].content == "file data here"

    def test_folds_error_tool_result(self):
        msgs = [
            MessageContent(
                role=MessageRole.assistant, type="tool_use",
                tool_name="Bash", tool_input={"cmd": "rm"}, tool_use_id="t2",
            ),
            MessageContent(
                role=MessageRole.tool, type="tool_result",
                content="Permission denied", tool_use_id="t2", is_error=True,
            ),
        ]
        result = _consolidate_messages(msgs)
        assert len(result) == 1
        assert "[error]" in result[0].content

    def test_empty_input(self):
        assert _consolidate_messages([]) == []

    def test_full_turn_consolidation(self):
        """A realistic turn: text + tool_use + tool_result + text merges cleanly."""
        msgs = [
            MessageContent(role=MessageRole.assistant, type="text", content="Let me check."),
            MessageContent(
                role=MessageRole.assistant, type="tool_use",
                tool_name="Read", tool_input={"path": "/f"}, tool_use_id="t1",
            ),
            MessageContent(
                role=MessageRole.tool, type="tool_result",
                content="contents", tool_use_id="t1",
            ),
            MessageContent(role=MessageRole.assistant, type="text", content="Found it!"),
        ]
        result = _consolidate_messages(msgs)
        # text, tool_use (with result folded in), text
        assert len(result) == 3
        assert result[0].type == "text"
        assert result[0].content == "Let me check."
        assert result[1].type == "tool_use"
        assert result[1].content == "contents"
        assert result[2].type == "text"
        assert result[2].content == "Found it!"


class TestParseJsonlLines:
    def test_full_conversation(self):
        lines = [
            _make_line("user", "user", "hello"),
            _make_line(
                "assistant",
                "assistant",
                [{"type": "text", "text": "Hi there!"}],
            ),
            _make_line(
                "assistant",
                "assistant",
                [
                    {
                        "type": "tool_use",
                        "name": "Read",
                        "input": {"path": "/f"},
                        "id": "t1",
                    }
                ],
            ),
            _make_line(
                "user",
                "user",
                [{"type": "tool_result", "tool_use_id": "t1", "content": "data"}],
            ),
            _make_line(
                "assistant",
                "assistant",
                [{"type": "text", "text": "Done!"}],
            ),
        ]
        result = parse_jsonl_lines(lines)
        assert result.metadata.session_id == "sess-1"
        # After consolidation: user, assistant text, tool_use (with result), assistant text
        assert len(result.messages) == 4
        assert result.messages[0].role == MessageRole.user
        assert result.messages[1].role == MessageRole.assistant
        assert result.messages[1].type == "text"
        assert result.messages[2].role == MessageRole.assistant
        assert result.messages[2].type == "tool_use"
        assert result.messages[2].content == "data"  # tool_result folded in
        assert result.messages[3].role == MessageRole.assistant
        assert result.messages[3].content == "Done!"

    def test_empty_input(self):
        result = parse_jsonl_lines([])
        assert result.messages == []
        assert result.metadata.session_id is None

    def test_filters_by_session_id(self):
        """Messages from a different sessionId (sidechains) are excluded."""
        lines = [
            _make_line("user", "user", "stray msg", session_id="other-sess"),
            _make_line("user", "user", "main msg", session_id="main-sess"),
            _make_line(
                "assistant",
                "assistant",
                [{"type": "text", "text": "reply"}],
                session_id="main-sess",
            ),
        ]
        # main-sess has 2 messages vs other-sess's 1, so it wins
        result = parse_jsonl_lines(lines)
        assert result.metadata.session_id == "main-sess"
        assert len(result.messages) == 2

    def test_filename_hint_overrides_count(self):
        """When called with a hint, it uses that session ID even if fewer messages."""
        lines = [
            _make_line("user", "user", "stray1", session_id="many"),
            _make_line("user", "user", "stray2", session_id="many"),
            _make_line("user", "user", "stray3", session_id="many"),
            _make_line("user", "user", "target msg", session_id="target"),
        ]
        result = parse_jsonl_lines(lines, session_id_hint="target")
        assert result.metadata.session_id == "target"
        assert len(result.messages) == 1

    def test_bad_json_skipped(self):
        lines = [
            "not valid json{{{",
            _make_line("user", "user", "good line"),
        ]
        result = parse_jsonl_lines(lines)
        assert len(result.messages) == 1


class TestParseJsonlFile:
    def test_file_io(self):
        lines = [
            _make_line("user", "user", "from file"),
            _make_line(
                "assistant",
                "assistant",
                [{"type": "text", "text": "response"}],
            ),
        ]
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".jsonl", delete=False
        ) as f:
            for line in lines:
                f.write(line + "\n")
            path = f.name

        try:
            result = parse_jsonl_file(path)
            assert len(result.messages) == 2
            assert result.metadata.session_id == "sess-1"
        finally:
            Path(path).unlink()

    def test_empty_file(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".jsonl", delete=False
        ) as f:
            path = f.name

        try:
            result = parse_jsonl_file(path)
            assert result.messages == []
        finally:
            Path(path).unlink()

    def test_filename_used_as_session_hint(self, tmp_path):
        """The filename stem should be used to resolve the correct session."""
        lines = [
            _make_line("user", "user", "stray", session_id="other-id"),
            _make_line("user", "user", "real msg", session_id="target-id"),
            _make_line(
                "assistant",
                "assistant",
                [{"type": "text", "text": "reply"}],
                session_id="target-id",
            ),
        ]
        path = tmp_path / "target-id.jsonl"
        path.write_text("\n".join(lines) + "\n")

        result = parse_jsonl_file(path)
        assert result.metadata.session_id == "target-id"
        assert len(result.messages) == 2
