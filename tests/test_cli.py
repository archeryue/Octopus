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
    do_pull,
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

    def test_pull_with_args(self):
        parser = build_parser()
        args = parser.parse_args([
            "pull",
            "session-42",
            "--server", "http://remote:9000",
            "--token", "secret",
            "--cwd", "/home/user/proj",
        ])
        assert args.command == "pull"
        assert args.session_id == "session-42"
        assert args.server == "http://remote:9000"
        assert args.token == "secret"
        assert args.cwd == "/home/user/proj"

    def test_pull_defaults(self):
        parser = build_parser()
        args = parser.parse_args(["pull", "sess-1"])
        assert args.command == "pull"
        assert args.session_id == "sess-1"
        assert args.server == "http://localhost:8000"
        assert args.token == "changeme"
        assert args.cwd is None
        assert args.project_dir is None

    def test_pull_project_dir_override(self):
        parser = build_parser()
        args = parser.parse_args([
            "pull", "sess-1",
            "--project-dir", "/custom/path",
        ])
        assert args.project_dir == "/custom/path"


class TestDoPull:
    def test_generates_uuid_when_no_claude_session_id(self, monkeypatch, capsys, tmp_path):
        """do_pull should generate a UUID and write JSONL when claude_session_id is missing."""

        # Mock urlopen to return a session without claude_session_id
        response_data = json.dumps({
            "id": "oct-123",
            "name": "Test",
            "working_dir": "/proj",
            "status": "idle",
            "created_at": "2026-01-01T00:00:00Z",
            "messages": [{"role": "user", "content": "hello", "type": "user"}],
        }).encode("utf-8")

        class FakeResponse:
            def __init__(self):
                self._data = response_data
            def read(self):
                return self._data
            def __enter__(self):
                return self
            def __exit__(self, *a):
                pass

        monkeypatch.setattr("urllib.request.urlopen", lambda req: FakeResponse())

        parser = build_parser()
        args = parser.parse_args(["pull", "oct-123", "--project-dir", str(tmp_path)])

        do_pull(args)

        captured = capsys.readouterr()
        assert "No claude_session_id on server" in captured.out
        assert "generated" in captured.out

        # Verify a JSONL file was written
        jsonl_files = list(tmp_path.glob("*.jsonl"))
        assert len(jsonl_files) == 1
