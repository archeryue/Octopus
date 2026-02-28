"""Tests for the CLI module."""

import json
import tempfile
from pathlib import Path

import pytest

from server.cli import (
    build_import_payload,
    build_parser,
    discover_sessions,
    get_project_dir,
)


class TestGetProjectDir:
    def test_escapes_slashes(self):
        result = get_project_dir("/home/user/my-project")
        assert result == Path.home() / ".claude" / "projects" / "-home-user-my-project"

    def test_root_dir(self):
        result = get_project_dir("/")
        assert result == Path.home() / ".claude" / "projects" / "-"


class TestDiscoverSessions:
    def test_empty_dir(self, tmp_path):
        sessions = discover_sessions(tmp_path)
        assert sessions == []

    def test_nonexistent_dir(self, tmp_path):
        sessions = discover_sessions(tmp_path / "nope")
        assert sessions == []

    def test_finds_jsonl_files(self, tmp_path):
        # Create a JSONL file with a user message
        data = {
            "type": "user",
            "sessionId": "abc-123",
            "cwd": "/test",
            "message": {"role": "user", "content": "hello world"},
        }
        (tmp_path / "abc-123.jsonl").write_text(json.dumps(data) + "\n")

        sessions = discover_sessions(tmp_path)
        assert len(sessions) == 1
        assert sessions[0]["session_id"] == "abc-123"
        assert sessions[0]["preview"] == "hello world"

    def test_ignores_non_jsonl(self, tmp_path):
        (tmp_path / "notes.txt").write_text("not a session")
        (tmp_path / "abc.jsonl").write_text(
            json.dumps({
                "type": "user",
                "sessionId": "abc",
                "message": {"role": "user", "content": "hi"},
            }) + "\n"
        )
        sessions = discover_sessions(tmp_path)
        assert len(sessions) == 1


class TestBuildImportPayload:
    def test_payload_structure(self, tmp_path):
        lines = [
            json.dumps({
                "type": "user",
                "sessionId": "sess-42",
                "cwd": "/home/user/proj",
                "timestamp": "2026-01-01T00:00:00Z",
                "message": {"role": "user", "content": "implement feature X"},
            }),
            json.dumps({
                "type": "assistant",
                "sessionId": "sess-42",
                "cwd": "/home/user/proj",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Sure!"}],
                },
            }),
        ]
        path = tmp_path / "sess-42.jsonl"
        path.write_text("\n".join(lines) + "\n")

        payload = build_import_payload(path)
        assert payload["claude_session_id"] == "sess-42"
        assert payload["working_dir"] == "/home/user/proj"
        assert "implement feature X" in payload["name"]
        assert len(payload["messages"]) == 2
        assert payload["messages"][0]["role"] == "user"
        assert payload["messages"][1]["role"] == "assistant"

    def test_custom_name(self, tmp_path):
        path = tmp_path / "s.jsonl"
        path.write_text(
            json.dumps({
                "type": "user",
                "sessionId": "s",
                "cwd": "/",
                "message": {"role": "user", "content": "yo"},
            }) + "\n"
        )
        payload = build_import_payload(path, name="My Custom Name")
        assert payload["name"] == "My Custom Name"


class TestBuildParser:
    def test_serve_default(self):
        parser = build_parser()
        args = parser.parse_args([])
        assert args.command is None

    def test_serve_explicit(self):
        parser = build_parser()
        args = parser.parse_args(["serve"])
        assert args.command == "serve"

    def test_handoff_with_args(self):
        parser = build_parser()
        args = parser.parse_args([
            "handoff",
            "--session-id", "abc-123",
            "--server", "http://remote:9000",
            "--token", "secret",
            "--name", "My Session",
        ])
        assert args.command == "handoff"
        assert args.session_id == "abc-123"
        assert args.server == "http://remote:9000"
        assert args.token == "secret"
        assert args.name == "My Session"
