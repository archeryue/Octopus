"""Gmail connector MCP server (connectors.md Phase C / §6.2).

Spawned as `python -m server.mcp_servers.connectors.gmail`. Mirrors the GitHub
server: per-call token from the host, projected + truncated results, 401 →
needs-reconnect, 429/5xx → bounded retry. send_draft actually sends — the
system-prompt blurb requires the model to confirm with the user first.
"""

from __future__ import annotations

import base64
import logging
import sys
import time
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import httpx

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mcp.server.fastmcp import FastMCP  # noqa: E402

from server.mcp_servers.connectors._shared import (  # noqa: E402
    ConnectorContext,
    to_text,
    truncate,
)

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s gmail-mcp %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)

mcp = FastMCP("octopus-gmail")
ctx = ConnectorContext()

_API = "https://gmail.googleapis.com/gmail/v1"
_RECONNECT_MSG = (
    "Error: Gmail token expired or revoked — ask the user to reconnect "
    "Gmail in Octopus's sidebar."
)


def _api(method: str, path: str, **kw: Any) -> tuple[Any | None, str | None]:
    """Authenticated Gmail call. Returns (parsed_body, None) or (None, error)."""
    token = ctx.access_token()
    if token is None:
        return None, "Error: connector unavailable — reconnect Gmail in Octopus."
    headers = {"Authorization": f"Bearer {token}"}
    url = path if path.startswith("http") else f"{_API}{path}"
    for attempt in range(3):
        try:
            r = httpx.request(method, url, headers=headers, timeout=30.0, **kw)
        except httpx.HTTPError as e:
            return None, f"Error: Gmail request failed: {e}"
        if r.status_code == 401:
            ctx.mark_needs_reconnect("invalid_grant")
            return None, _RECONNECT_MSG
        if r.status_code == 429 or r.status_code >= 500:
            if attempt < 2:
                time.sleep(min(float(r.headers.get("Retry-After", "1") or 1), 5.0))
                continue
            return None, f"Error: Gmail temporarily unavailable ({r.status_code})."
        if r.status_code >= 400:
            return None, f"Error: Gmail {r.status_code}: {r.text[:300]}"
        return (r.json() if r.content else {}), None
    return None, "Error: Gmail rate-limited; try again shortly."


def _b64url_decode(data: str) -> bytes:
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad)


def _header(payload: dict, name: str) -> str:
    for h in payload.get("headers", []):
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def _find_plain(payload: dict) -> str | None:
    """Depth-first search for a text/plain part anywhere in the tree."""
    body = payload.get("body", {})
    if payload.get("mimeType") == "text/plain" and body.get("data"):
        return _b64url_decode(body["data"]).decode("utf-8", "replace")
    for part in payload.get("parts", []) or []:
        found = _find_plain(part)
        if found is not None:
            return found
    return None


def _any_body(payload: dict) -> str:
    """First inline body anywhere — fallback when there's no text/plain part
    (e.g. an HTML-only message)."""
    data = payload.get("body", {}).get("data")
    if data:
        return _b64url_decode(data).decode("utf-8", "replace")
    for part in payload.get("parts", []) or []:
        text = _any_body(part)
        if text:
            return text
    return ""


def _extract_text(payload: dict) -> str:
    """Prefer text/plain (searched across the whole tree so it wins over a
    text/html sibling that appears first); fall back to any inline body."""
    plain = _find_plain(payload)
    return plain if plain is not None else _any_body(payload)


@mcp.tool(name="search")
def search(query: str, limit: int = 10) -> str:
    """Search messages with Gmail query syntax (e.g. 'from:alice is:unread
    newer_than:7d'). Returns brief headers + snippet per match."""
    listing, err = _api(
        "GET", "/users/me/messages", params={"q": query, "maxResults": min(limit, 25)}
    )
    if err:
        return err
    out = []
    for ref in listing.get("messages", []) or []:
        msg, merr = _api(
            "GET",
            f"/users/me/messages/{ref['id']}",
            params={
                "format": "metadata",
                "metadataHeaders": ["Subject", "From", "Date"],
            },
        )
        if merr:
            continue
        out.append(
            {
                "id": msg.get("id"),
                "thread_id": msg.get("threadId"),
                "from": _header(msg.get("payload", {}), "From"),
                "subject": _header(msg.get("payload", {}), "Subject"),
                "date": _header(msg.get("payload", {}), "Date"),
                "snippet": msg.get("snippet", ""),
            }
        )
    return truncate(to_text(out))


@mcp.tool(name="get")
def get(message_id: str) -> str:
    """Full message: headers + decoded plain-text body."""
    msg, err = _api("GET", f"/users/me/messages/{message_id}", params={"format": "full"})
    if err:
        return err
    payload = msg.get("payload", {})
    out = {
        "id": msg.get("id"),
        "thread_id": msg.get("threadId"),
        "from": _header(payload, "From"),
        "to": _header(payload, "To"),
        "subject": _header(payload, "Subject"),
        "date": _header(payload, "Date"),
        "body": _extract_text(payload),
    }
    return truncate(to_text(out))


@mcp.tool(name="list_labels")
def list_labels() -> str:
    """The account's labels (id + name) — needed for label/unlabel."""
    data, err = _api("GET", "/users/me/labels")
    if err:
        return err
    labels = [{"id": lbl.get("id"), "name": lbl.get("name")} for lbl in data.get("labels", [])]
    return truncate(to_text(labels))


def _build_raw(to: str, subject: str, body: str, in_reply_to: str | None) -> str:
    msg = EmailMessage()
    msg["To"] = to
    msg["Subject"] = subject
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    msg.set_content(body)
    return base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")


@mcp.tool(name="create_draft")
def create_draft(
    to: str, subject: str, body: str, in_reply_to_message_id: str | None = None
) -> str:
    """Compose a draft (does NOT send). Returns the draft id to pass to
    send_draft after the user approves."""
    raw = _build_raw(to, subject, body, in_reply_to_message_id)
    data, err = _api("POST", "/users/me/drafts", json={"message": {"raw": raw}})
    if err:
        return err
    return to_text({"draft_id": data.get("id"), "to": to, "subject": subject})


@mcp.tool(name="send_draft")
def send_draft(draft_id: str) -> str:
    """Send a previously-created draft. REQUIRES explicit user confirmation in
    the same turn (ask via mcp__ask__user first) — this delivers email."""
    data, err = _api("POST", "/users/me/drafts/send", json={"id": draft_id})
    if err:
        return err
    return to_text({"sent": True, "message_id": data.get("id")})


@mcp.tool(name="label")
def label(message_id: str, label_ids: list[str]) -> str:
    """Add labels to a message (label ids from list_labels)."""
    data, err = _api(
        "POST",
        f"/users/me/messages/{message_id}/modify",
        json={"addLabelIds": label_ids},
    )
    if err:
        return err
    return to_text({"id": data.get("id"), "labels": data.get("labelIds", [])})


@mcp.tool(name="unlabel")
def unlabel(message_id: str, label_ids: list[str]) -> str:
    """Remove labels from a message (label ids from list_labels)."""
    data, err = _api(
        "POST",
        f"/users/me/messages/{message_id}/modify",
        json={"removeLabelIds": label_ids},
    )
    if err:
        return err
    return to_text({"id": data.get("id"), "labels": data.get("labelIds", [])})


if __name__ == "__main__":
    mcp.run()
