"""Write Octopus sessions back to Claude Code JSONL format for local resumption."""

from __future__ import annotations

import json
import uuid as uuid_mod
from datetime import datetime, timezone
from pathlib import Path

from .models import MessageContent

# Match the Claude Code JSONL version field
_JSONL_VERSION = "2.1.62"


def _message_to_jsonl_line(
    msg: MessageContent, session_id: str, cwd: str
) -> dict | None:
    """Convert a single MessageContent back to a Claude Code JSONL line."""
    base = {"sessionId": session_id, "cwd": cwd}

    if msg.role == "user" and msg.type == "text":
        return {
            **base,
            "type": "user",
            "message": {"role": "user", "content": msg.content or ""},
        }

    if msg.role == "tool" and msg.type == "tool_result":
        line: dict = {
            **base,
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": msg.tool_use_id,
                        "content": msg.content,
                    }
                ],
            },
        }
        if msg.is_error:
            line["message"]["content"][0]["is_error"] = True
        return line

    if msg.role == "assistant" and msg.type == "text":
        return {
            **base,
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": msg.content or ""}],
            },
        }

    if msg.role == "assistant" and msg.type == "tool_use":
        return {
            **base,
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": msg.tool_use_id,
                        "name": msg.tool_name,
                        "input": msg.tool_input or {},
                    }
                ],
            },
        }

    return None


def write_jsonl_file(
    path: Path,
    messages: list[MessageContent],
    session_id: str,
    cwd: str,
) -> None:
    """Write a list of MessageContent objects to a Claude Code JSONL file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    parent_uuid: str | None = None
    with path.open("w", encoding="utf-8") as f:
        for msg in messages:
            line = _message_to_jsonl_line(msg, session_id, cwd)
            if line is not None:
                line_uuid = uuid_mod.uuid4().hex
                line["uuid"] = line_uuid
                line["parentUuid"] = parent_uuid
                line["version"] = _JSONL_VERSION
                line["timestamp"] = datetime.now(timezone.utc).isoformat()
                line["isSidechain"] = False
                line["userType"] = "external"
                f.write(json.dumps(line) + "\n")
                parent_uuid = line_uuid
