"""Prompt builders + tolerant JSON parsing for the research pipeline
(native-deep-research.md §3). Pure functions — no I/O, no model — so the whole
shape is unit-testable without a backend.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def _extract_json(text: str) -> Any:
    """Pull the first JSON array or object out of a model reply, tolerating
    ```json fences and surrounding prose. Returns the parsed value or None."""
    if not text:
        return None
    candidates: list[str] = []
    m = _FENCE_RE.search(text)
    if m:
        candidates.append(m.group(1).strip())
    candidates.append(text.strip())
    # Also try the widest [...] / {...} span.
    for open_c, close_c in (("[", "]"), ("{", "}")):
        i, j = text.find(open_c), text.rfind(close_c)
        if 0 <= i < j:
            candidates.append(text[i : j + 1])
    for c in candidates:
        try:
            return json.loads(c)
        except (json.JSONDecodeError, TypeError):
            continue
    return None


@dataclass
class Finding:
    claim: str
    url: str
    angle: str = ""


# ------------------------------------------------------------------ scope


def scope_prompt(question: str, max_angles: int) -> str:
    return (
        "You are planning web research. Decompose the QUESTION into at most "
        f"{max_angles} distinct, specific search angles that together cover it. "
        "Return ONLY a JSON array of short search-query strings (no prose, no "
        "markdown fences).\n\nQUESTION: " + question
    )


def parse_angles(text: str, *, question: str, max_angles: int) -> list[str]:
    """Parse the scope reply into a list of angle strings; fall back to the
    raw question if the model didn't return usable JSON."""
    val = _extract_json(text)
    angles: list[str] = []
    if isinstance(val, list):
        for item in val:
            if isinstance(item, str) and item.strip():
                angles.append(item.strip())
            elif isinstance(item, dict):
                s = item.get("angle") or item.get("query") or item.get("q")
                if isinstance(s, str) and s.strip():
                    angles.append(s.strip())
    angles = angles[:max_angles]
    return angles or [question]


# ------------------------------------------------------------------ search


def search_prompt(angle: str, question: str, max_findings: int) -> str:
    return (
        "Use your web tools to research the ANGLE below (it serves the overall "
        "QUESTION). Find authoritative sources and extract up to "
        f"{max_findings} specific, verifiable factual claims, each with the "
        "source URL you found it in. Return ONLY a JSON array of objects with "
        'keys "claim" and "url" (no prose, no markdown fences).\n\n'
        f"ANGLE: {angle}\nQUESTION: {question}"
    )


def parse_findings(text: str, *, angle: str, max_findings: int) -> list[Finding]:
    val = _extract_json(text)
    out: list[Finding] = []
    if isinstance(val, list):
        for item in val:
            if not isinstance(item, dict):
                continue
            claim = item.get("claim")
            url = item.get("url") or item.get("source") or ""
            if isinstance(claim, str) and claim.strip():
                out.append(
                    Finding(
                        claim=claim.strip(),
                        url=(url.strip() if isinstance(url, str) else ""),
                        angle=angle,
                    )
                )
            if len(out) >= max_findings:
                break
    return out


# ------------------------------------------------------------------ dedup + rank


def _norm_claim(claim: str) -> str:
    return re.sub(r"\s+", " ", claim.lower()).strip(" .")


def dedup_and_rank(findings: list[Finding], *, max_claims: int) -> list[Finding]:
    """Drop near-duplicate claims (normalized text), keeping the first; rank so
    claims corroborated across MORE angles come first, then cap. Pure."""
    by_key: dict[str, Finding] = {}
    angles_for: dict[str, set[str]] = {}
    for f in findings:
        if not f.claim:
            continue
        k = _norm_claim(f.claim)
        if not k:
            continue
        if k not in by_key:
            by_key[k] = f
            angles_for[k] = set()
        if f.angle:
            angles_for[k].add(f.angle)
    ranked = sorted(
        by_key.items(), key=lambda kv: len(angles_for[kv[0]]), reverse=True
    )
    return [f for _, f in ranked[:max_claims]]


# ------------------------------------------------------------------ verify


def verify_prompt(claim: str, url: str, question: str) -> str:
    return (
        "Fact-check the CLAIM using your web tools. Actively search for evidence "
        "that CONTRADICTS it. Return ONLY a JSON object with keys "
        '"refuted" (true/false) and "note" (one short sentence). Mark refuted '
        "true ONLY if you find credible contradicting evidence; if it checks "
        "out or is merely unverifiable, refuted is false.\n\n"
        f"CLAIM: {claim}\nSOURCE: {url}\nQUESTION: {question}"
    )


def parse_verdict(text: str) -> bool:
    """Return True if the verifier REFUTED the claim. Defaults to NOT refuted
    when the reply is unparseable (don't kill a claim on a parse failure)."""
    val = _extract_json(text)
    if isinstance(val, dict):
        r = val.get("refuted")
        if isinstance(r, bool):
            return r
        if isinstance(r, str):
            return r.strip().lower() in ("true", "yes", "1")
    return False


# ------------------------------------------------------------------ synthesize


def synthesize_prompt(question: str, findings: list[Finding]) -> str:
    bullets = "\n".join(
        f"- {f.claim}" + (f"  [source: {f.url}]" if f.url else "")
        for f in findings
    )
    if not bullets:
        bullets = "(no verified findings)"
    return (
        "Write a concise, well-structured research report that answers the "
        "QUESTION using ONLY the VERIFIED FINDINGS below. Cite sources inline "
        "as [url]. If the findings are insufficient, say so plainly. Use "
        "Markdown.\n\n"
        f"QUESTION: {question}\n\nVERIFIED FINDINGS:\n{bullets}"
    )
