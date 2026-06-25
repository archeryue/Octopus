"""Resolve a `/showme <reference>` user input to a concrete file path.

Three layers, cheapest first:

  1. **Exact-path short-circuit.** If `<reference>` is already a real file in
     the session's working directory, return it directly — no model call.
  2. **One-shot model call.** Ask the session's harness (via `run_oneshot`)
     to interpret the reference using recent conversation context. Tolerates
     fences and surrounding prose when extracting the JSON object (the same
     pattern `schedule_ai.extract_json` uses).
  3. **Bare-path fallback.** If the model returns a single path-shaped token
     rather than JSON, accept it. Lots of one-shot replies look like
     `README.md` rather than `{"path":"README.md"}` and that's a legit
     resolution.

Layer 3 is what saved us from breaking on `/showme the readme` — the model
often replies with just the path even when told to use JSON.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Any

from .harness import HarnessCredential, OneShotContext

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ShowMeResolution:
    path: str | None
    message: str | None = None


# Path-like heuristic for the bare-token fallback: a filename with an
# extension, or a relative path made of segments like that. Deliberately
# narrow so prose like "the README" doesn't get treated as a path.
_PATH_LIKE_RE = re.compile(r"^[\w./\-]+\.[A-Za-z0-9]{1,8}$")


def resolve_local_path(text: str, working_dir: str) -> str | None:
    """Return `text` if it already resolves to a file inside `working_dir`,
    else None. No model call. Sandboxed: rejects anything that resolves
    outside `working_dir` (defends against `../etc/passwd` etc.)."""
    text = (text or "").strip()
    if not text or "\n" in text:
        return None
    try:
        wd_real = os.path.realpath(working_dir)
        cand_real = os.path.realpath(os.path.join(wd_real, text))
    except (OSError, ValueError):
        return None
    if not cand_real.startswith(wd_real + os.sep) and cand_real != wd_real:
        return None
    return text if os.path.isfile(cand_real) else None


def extract_json(model_text: str) -> dict | None:
    """Pull a JSON object out of the model's reply, tolerating ```json
    fences and surrounding prose. Returns None if no object is parseable."""
    s = (model_text or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\n?", "", s)
        s = re.sub(r"\n?```$", "", s).strip()
    start = s.find("{")
    end = s.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        obj = json.loads(s[start : end + 1])
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def _bare_path_fallback(model_text: str) -> str | None:
    """If the model replied with a single path-shaped token (very common —
    `claude --print` often just answers `README.md`), use it as a path."""
    s = (model_text or "").strip().strip("`'\"")
    # First non-empty line only — the model sometimes adds a trailing
    # confirmation sentence after the path.
    first = next((ln.strip().strip("`'\"") for ln in s.splitlines() if ln.strip()), "")
    return first if first and _PATH_LIKE_RE.match(first) else None


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


def _build_prompt(text: str, working_dir: str, session_name: str | None, messages_blurb: str) -> str:
    return (
        "Resolve the user's `/showme` reference to a concrete file path.\n"
        "\n"
        "Respond with a valid JSON object ONLY — no prose before or after, "
        "no markdown fences. Choose exactly ONE shape:\n"
        '  {"path": "<relative path inside the working directory>"}\n'
        '  {"message": "<short clarifying question if too ambiguous>"}\n'
        "\n"
        "Rules:\n"
        "- Prefer the single most likely file the user means, based on the "
        "reference and recent conversation.\n"
        '- Common conventions: "the readme" → README.md; "the changelog" → '
        "CHANGELOG.md; etc.\n"
        "- Paths are RELATIVE to the working directory.\n"
        '- Only return {"message": ...} when the reference is genuinely '
        "ambiguous — don't ask for clarification when a sensible guess exists.\n"
        "\n"
        f"Working directory: {working_dir}\n"
        f"Session: {session_name or '(unnamed)'}\n"
        f"User reference: {text}\n"
        "\n"
        "Recent conversation:\n"
        f"{messages_blurb or '(none — this is the first interaction)'}\n"
        "\n"
        "Your JSON response:"
    )


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
    """Resolve a human file reference to a concrete path. See module docstring
    for the three layers (exact-path short-circuit, model call, path-shaped
    fallback)."""
    # Layer 1 — exact-path short-circuit. Saves a model call for the common
    # case and means `/showme README.md` can't fail just because the model
    # got chatty.
    direct = resolve_local_path(text, working_dir)
    if direct is not None:
        return ShowMeResolution(path=direct)

    # Layer 2 — one-shot model call.
    prompt = _build_prompt(text, working_dir, session_name, _format_messages(messages))
    ctx = OneShotContext(
        prompt=prompt, model=model, credential=credential, working_dir=working_dir
    )
    out = await harness.run_oneshot(ctx)

    # Track whatever path the model identified, even if it turns out not to
    # exist on disk — lets us emit a precise "file not found" message rather
    # than the generic "couldn't pin down a file" fallback.
    candidate_path: str | None = None

    obj = extract_json(out)
    if obj is not None:
        path = obj.get("path")
        message = obj.get("message")
        if isinstance(path, str) and path.strip():
            candidate_path = path.strip()
            if resolve_local_path(candidate_path, working_dir) is not None:
                return ShowMeResolution(path=candidate_path)
            # Path from model doesn't exist on disk — fall through to the
            # "file not found" error below; don't try bare-path on the same out.
        elif isinstance(message, str) and message.strip():
            return ShowMeResolution(path=None, message=message.strip())

    # Layer 3 — bare-path fallback. The model often replies with just the
    # path as a single token instead of the requested JSON wrapper; accept it.
    # Only reached when JSON extraction found nothing (obj is None, or the
    # object had neither a valid path nor a clarifying message).
    if candidate_path is None:
        bare = _bare_path_fallback(out)
        if bare is not None:
            candidate_path = bare
            if resolve_local_path(bare, working_dir) is not None:
                return ShowMeResolution(path=bare)
            # Bare path doesn't exist either — fall through.

    if candidate_path is not None:
        logger.warning(
            "showme: model returned non-existent path (text=%r, path=%r, wd=%r)",
            text,
            candidate_path,
            working_dir,
        )
        return ShowMeResolution(
            path=None,
            message=f'Couldn\'t find "{candidate_path}" in the working directory. Try giving the path directly.',
        )

    logger.warning(
        "showme: couldn't parse model output (text=%r, len=%d): %r",
        text,
        len(out or ""),
        (out or "")[:400],
    )
    return ShowMeResolution(
        path=None,
        message="Couldn't pin down a file from that reference. Try giving the path directly.",
    )
