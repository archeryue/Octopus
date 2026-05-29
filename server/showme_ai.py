from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .harness import HarnessCredential, OneShotContext


@dataclass(frozen=True)
class ShowMeResolution:
    path: str | None
    message: str | None = None


def _format_messages(messages: list[dict[str, Any]], limit: int = 12) -> str:
    lines: list[str] = []
    for msg in messages[-limit:]:
        role = msg.get("role")
        typ = msg.get("type")
        text = msg.get("content")
        if role == "user" and typ == "text" and isinstance(text, str):
            lines.append(f"User: {text}")
        elif role == "assistant" and typ == "text" and isinstance(text, str):
            lines.append(f"Assistant: {text}")
        elif typ == "tool_use":
            tool = msg.get("tool_name") or ""
            tool_input = msg.get("tool_input") or {}
            if isinstance(tool_input, dict) and isinstance(tool_input.get("path"), str):
                lines.append(f"Assistant used {tool} with path={tool_input['path']!r}")
    return "\n".join(lines)


async def resolve_showme_reference(
    text: str,
    *,
    harness,
    model: str | None,
    credential: HarnessCredential | None,
    working_dir: str,
    messages: list[dict[str, Any]],
    session_name: str | None = None,
) -> ShowMeResolution:
    """Use a one-shot model call to resolve a human file reference.

    The model sees the recent conversation context and returns JSON with
    either a concrete `path` or a `message` telling the user it needs more
    detail. The client is responsible for opening the viewer when a path is
    returned.
    """
    prompt = (
        "You are resolving the user's `/showme` request for Octopus.\n"
        "Interpret the user reference using the conversation context and return "
        "ONLY valid JSON.\n\n"
        "Return one of:\n"
        '  {"path": "relative/path/to/file"}\n'
        '  {"message": "short clarification question or explanation"}\n\n'
        "Rules:\n"
        "- Prefer the single most likely file mentioned or implied by the conversation.\n"
        "- Return a path relative to the working directory.\n"
        "- Do not wrap the JSON in markdown fences.\n"
        "- If the request is too ambiguous, return a short clarifying question.\n\n"
        f"Working directory: {working_dir}\n"
        f"Session: {session_name or '(unnamed)'}\n"
        f"User input: /showme {text}\n\n"
        "Recent conversation:\n"
        f"{_format_messages(messages)}"
    )
    ctx = OneShotContext(prompt=prompt, model=model, credential=credential, working_dir=working_dir)
    out = await harness.run_oneshot(ctx)

    raw = out.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.startswith("json"):
            raw = raw[4:].strip()
    try:
        data = json.loads(raw)
    except Exception:
        return ShowMeResolution(path=None, message="The model returned an unexpected response. Try rephrasing the file reference.")

    path = data.get("path") if isinstance(data, dict) else None
    message = data.get("message") if isinstance(data, dict) else None
    if isinstance(path, str) and path.strip():
        return ShowMeResolution(path=path.strip())
    if isinstance(message, str) and message.strip():
        return ShowMeResolution(path=None, message=message.strip())
    return ShowMeResolution(path=None, message="The model couldn't resolve that file reference. Try naming the file more directly.")
