"""Parse Claude Code JSONL session files into Octopus MessageContent format."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .models import MessageContent, MessageRole


@dataclass
class SessionMetadata:
    session_id: str | None = None
    cwd: str | None = None
    first_user_message: str | None = None
    timestamp: str | None = None


@dataclass
class ParsedSession:
    metadata: SessionMetadata = field(default_factory=SessionMetadata)
    messages: list[MessageContent] = field(default_factory=list)


def _convert_line(data: dict[str, Any]) -> list[MessageContent] | None:
    """Convert a single JSONL line to Octopus MessageContent list.

    Returns None for lines that should be skipped.
    """
    line_type = data.get("type")
    message = data.get("message")

    if line_type not in ("user", "assistant") or message is None:
        return None

    role_str = message.get("role")
    content = message.get("content")

    if role_str == "user":
        if isinstance(content, str):
            return [MessageContent(role=MessageRole.user, type="text", content=content)]
        if isinstance(content, list):
            results = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                block_type = block.get("type")
                if block_type == "text":
                    results.append(
                        MessageContent(
                            role=MessageRole.user,
                            type="text",
                            content=block.get("text", ""),
                        )
                    )
                elif block_type == "tool_result":
                    results.append(
                        MessageContent(
                            role=MessageRole.tool,
                            type="tool_result",
                            content=block.get("content"),
                            tool_use_id=block.get("tool_use_id"),
                            is_error=block.get("is_error") or None,
                        )
                    )
            return results if results else None

    elif role_str == "assistant":
        if isinstance(content, list):
            results = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                block_type = block.get("type")
                if block_type == "text":
                    results.append(
                        MessageContent(
                            role=MessageRole.assistant,
                            type="text",
                            content=block.get("text", ""),
                        )
                    )
                elif block_type == "tool_use":
                    results.append(
                        MessageContent(
                            role=MessageRole.assistant,
                            type="tool_use",
                            tool_name=block.get("name"),
                            tool_input=block.get("input"),
                            tool_use_id=block.get("id"),
                        )
                    )
            return results if results else None

    return None


def _find_primary_session_id(
    lines: list[dict[str, Any]], hint: str | None = None
) -> str | None:
    """Determine the primary session ID from JSONL lines.

    JSONL files can contain stray messages from other sessions (e.g. resumed
    sessions). Use the filename stem as a hint when available; otherwise fall
    back to the session ID with the most messages.
    """
    from collections import Counter

    counts: Counter[str] = Counter()
    for data in lines:
        sid = data.get("sessionId")
        if sid and data.get("type") in ("user", "assistant"):
            counts[sid] += 1

    if not counts:
        return None

    # If the hint matches a known session ID, prefer it
    if hint and hint in counts:
        return hint

    # Otherwise pick the session ID with the most messages
    return counts.most_common(1)[0][0]


def _extract_session_metadata(
    lines: list[dict[str, Any]], session_id_hint: str | None = None
) -> SessionMetadata:
    """Extract session ID, cwd, and first user message from parsed JSONL lines."""
    meta = SessionMetadata()
    meta.session_id = _find_primary_session_id(lines, hint=session_id_hint)

    for data in lines:
        if data.get("type") not in ("user", "assistant"):
            continue
        if meta.session_id and data.get("sessionId") != meta.session_id:
            continue
        if meta.cwd is None and data.get("cwd"):
            meta.cwd = data["cwd"]
        if meta.timestamp is None and data.get("timestamp"):
            meta.timestamp = data["timestamp"]
        if meta.first_user_message is None and data.get("type") == "user":
            message = data.get("message", {})
            content = message.get("content")
            if isinstance(content, str):
                meta.first_user_message = content[:200]
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        meta.first_user_message = block.get("text", "")[:200]
                        break
        if all([meta.cwd, meta.first_user_message, meta.timestamp]):
            break
    return meta


def _consolidate_messages(messages: list[MessageContent]) -> list[MessageContent]:
    """Merge consecutive same-role text blocks and collapse tool call pairs.

    Produces a cleaner conversation view:
    - Consecutive assistant text messages are merged into one
    - tool_use + its matching tool_result are collapsed into a single tool_use
      with a summary (keeps the conversation readable without losing context)
    - Consecutive user text messages are merged into one
    """
    if not messages:
        return []

    # First pass: build a lookup of tool_use_id -> tool_result
    result_by_id: dict[str, MessageContent] = {}
    for msg in messages:
        if msg.type == "tool_result" and msg.tool_use_id:
            result_by_id[msg.tool_use_id] = msg

    consolidated: list[MessageContent] = []
    for msg in messages:
        # Skip standalone tool_results — they'll be folded into tool_use
        if msg.type == "tool_result" and msg.tool_use_id in result_by_id:
            continue

        # For tool_use, attach a content summary from its result
        if msg.type == "tool_use" and msg.tool_use_id:
            result = result_by_id.get(msg.tool_use_id)
            result_preview = None
            if result and result.content:
                preview = str(result.content)[:200]
                if result.is_error:
                    result_preview = f"[error] {preview}"
                else:
                    result_preview = preview
            consolidated.append(MessageContent(
                role=msg.role,
                type=msg.type,
                content=result_preview,
                tool_name=msg.tool_name,
                tool_input=msg.tool_input,
                tool_use_id=msg.tool_use_id,
            ))
            continue

        # Merge consecutive text messages with the same role
        if (
            msg.type == "text"
            and consolidated
            and consolidated[-1].type == "text"
            and consolidated[-1].role == msg.role
        ):
            prev = consolidated[-1]
            merged = (prev.content or "") + "\n\n" + (msg.content or "")
            consolidated[-1] = MessageContent(
                role=prev.role,
                type="text",
                content=merged,
            )
            continue

        consolidated.append(msg)

    return consolidated


def parse_jsonl_lines(
    lines: list[str], session_id_hint: str | None = None
) -> ParsedSession:
    """Parse JSONL string lines into a ParsedSession."""
    parsed_data: list[dict[str, Any]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            parsed_data.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    metadata = _extract_session_metadata(parsed_data, session_id_hint=session_id_hint)
    messages: list[MessageContent] = []
    # Only include messages matching the primary session ID
    for data in parsed_data:
        if metadata.session_id and data.get("sessionId") != metadata.session_id:
            continue
        converted = _convert_line(data)
        if converted:
            messages.extend(converted)

    messages = _consolidate_messages(messages)
    return ParsedSession(metadata=metadata, messages=messages)


def parse_jsonl_file(path: Path | str) -> ParsedSession:
    """Read a JSONL file and parse it into a ParsedSession."""
    path = Path(path)
    with path.open("r", encoding="utf-8") as f:
        lines = f.readlines()
    return parse_jsonl_lines(lines, session_id_hint=path.stem)
