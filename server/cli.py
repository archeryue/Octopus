"""CLI entry point for Octopus: serve (default) and handoff subcommands."""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
import urllib.error
from pathlib import Path


def get_project_dir(cwd: str | None = None) -> Path:
    """Return the Claude Code project directory for the given (or current) cwd."""
    if cwd is None:
        cwd = str(Path.cwd())
    escaped = cwd.replace("/", "-").replace("\\", "-")
    return Path.home() / ".claude" / "projects" / escaped


def discover_sessions(project_dir: Path) -> list[dict]:
    """Find JSONL session files and return basic info about each."""
    if not project_dir.is_dir():
        return []

    sessions = []
    for jsonl_path in sorted(project_dir.glob("*.jsonl")):
        session_id = jsonl_path.stem
        preview = _get_session_preview(jsonl_path)
        sessions.append({
            "session_id": session_id,
            "path": jsonl_path,
            "preview": preview,
        })
    return sessions


def _get_session_preview(path: Path) -> str:
    """Extract first user message from a JSONL file as a preview."""
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if data.get("type") != "user":
                    continue
                message = data.get("message", {})
                content = message.get("content")
                if isinstance(content, str):
                    return content[:100]
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            return block.get("text", "")[:100]
    except OSError:
        pass
    return "(no preview)"


def build_import_payload(
    jsonl_path: Path, name: str | None = None
) -> dict:
    """Parse a JSONL file and build the import API payload."""
    from .jsonl_parser import parse_jsonl_file

    parsed = parse_jsonl_file(jsonl_path)
    meta = parsed.metadata

    return {
        "name": name or f"Handoff: {(meta.first_user_message or 'session')[:60]}",
        "working_dir": meta.cwd,
        "claude_session_id": meta.session_id,
        "messages": [msg.model_dump(exclude_none=True) for msg in parsed.messages],
    }


def do_handoff(args: argparse.Namespace) -> None:
    """Execute the handoff subcommand."""
    server = args.server.rstrip("/")
    token = args.token

    # Determine project dir
    project_dir = Path(args.project_dir) if args.project_dir else get_project_dir()

    if args.session_id:
        jsonl_path = project_dir / f"{args.session_id}.jsonl"
        if not jsonl_path.exists():
            print(f"Error: Session file not found: {jsonl_path}", file=sys.stderr)
            sys.exit(1)
    else:
        # Discover and let user pick
        sessions = discover_sessions(project_dir)
        if not sessions:
            print(f"No sessions found in {project_dir}", file=sys.stderr)
            sys.exit(1)

        print(f"Found {len(sessions)} session(s) in {project_dir}:\n")
        for i, s in enumerate(sessions, 1):
            print(f"  {i}. {s['session_id'][:12]}...  {s['preview']}")
        print()

        try:
            choice = input(f"Select session (1-{len(sessions)}): ").strip()
            idx = int(choice) - 1
            if idx < 0 or idx >= len(sessions):
                raise ValueError
        except (ValueError, EOFError, KeyboardInterrupt):
            print("\nAborted.", file=sys.stderr)
            sys.exit(1)

        jsonl_path = sessions[idx]["path"]

    # Build payload
    payload = build_import_payload(jsonl_path, name=args.name)
    msg_count = len(payload["messages"])
    print(f"Importing session with {msg_count} messages...")

    # POST to server
    url = f"{server}/api/sessions/import"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            print(f"Session imported: {result['id']}")
            print(f"  Name: {result['name']}")
            print(f"  Messages: {result['message_count']}")
            print(f"  URL: {server}/sessions/{result['id']}")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"Error: HTTP {e.code} — {body}", file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as e:
        print(f"Error: Could not connect to {server} — {e.reason}", file=sys.stderr)
        sys.exit(1)


def do_serve(args: argparse.Namespace) -> None:
    """Execute the serve subcommand (default)."""
    from .main import run
    run()


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="octopus",
        description="Octopus — remote Claude Code controller",
    )
    subparsers = parser.add_subparsers(dest="command")

    # serve (default)
    subparsers.add_parser("serve", help="Start the Octopus server")

    # handoff
    handoff_parser = subparsers.add_parser(
        "handoff", help="Import a local Claude Code session"
    )
    handoff_parser.add_argument(
        "--session-id",
        help="Claude Code session UUID (skips interactive selection)",
    )
    handoff_parser.add_argument(
        "--project-dir",
        help="Path to Claude Code project directory (default: auto-detect from cwd)",
    )
    handoff_parser.add_argument(
        "--server",
        default="http://localhost:8000",
        help="Octopus server URL (default: http://localhost:8000)",
    )
    handoff_parser.add_argument(
        "--token",
        default="changeme",
        help="Auth token for the Octopus server",
    )
    handoff_parser.add_argument(
        "--name",
        help="Name for the imported session",
    )

    return parser


def main() -> None:
    """CLI entry point."""
    parser = build_parser()
    args = parser.parse_args()

    if args.command is None or args.command == "serve":
        do_serve(args)
    elif args.command == "handoff":
        do_handoff(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
