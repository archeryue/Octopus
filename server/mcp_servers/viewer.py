"""MCP stdio server exposing one tool: `show_file`.

Claude calls `show_file(path)` either because the user typed
`/showme <path>` in chat or because the model decided opening a
file would help the user. The tool validates the path against the
session's working directory (passed via `OCTOPUS_WORKING_DIR` env
when we spawn this server) and returns a human-readable confirmation
that the model relays into its next reply.

The actual rendering happens in the browser: the frontend watches
the chat stream for tool_use events named
`mcp__viewer__show_file`, and when one arrives it pops the
FileViewerDialog with the same `path` arg, which then GETs
`/api/sessions/{id}/files?path=...` to fetch bytes. The MCP tool's
return value is purely for the model's benefit (so it knows whether
to apologize for a wrong path, etc.).

Spawned as: `OCTOPUS_WORKING_DIR=/abs/path python -m server.mcp_servers.viewer`
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

# When claude spawns us, our cwd is the user's working_dir but
# server.* won't be importable unless PYTHONPATH was set. The backend
# sets PYTHONPATH explicitly, but harden against missing setups by
# adding the repo root (parent of `server/`) up front.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mcp.server.fastmcp import FastMCP  # noqa: E402

from server.file_viewer import (  # noqa: E402
    FileViewerError,
    resolve_safe_path,
)

# stdout is reserved for MCP protocol frames. Send logs to stderr so
# they don't corrupt the wire format.
logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s viewer-mcp %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)


mcp = FastMCP("octopus-viewer")


@mcp.tool()
def show_file(path: str) -> str:
    """Open a file from the session's working directory in the user's
    in-app viewer modal.

    Use this when the user types `/showme <path>` in chat, or
    proactively when revealing a file directly to the user would be
    clearer than quoting it back in your reply. Supported file types:
    Markdown (.md), code files (Python, JS/TS, Go, Rust, etc.),
    images (PNG/JPG/GIF/SVG/WebP), PDFs, and plain text/log/CSV.

    Args:
        path: The path to open, relative to the session's working
            directory (e.g. "docs/plan.md" or "src/main.py").
            Absolute paths inside the working dir are also accepted.
            If you're unsure of the exact filename — typo, wrong
            extension, partial name — use Glob or Read first to find
            the actual file, then call show_file with the correct path.
    """
    working_dir = os.environ.get("OCTOPUS_WORKING_DIR")
    if not working_dir:
        # Bug in our spawn config — make it loud, not silent. The model
        # can't recover from this, so don't dress it up as a path issue.
        logger.error("OCTOPUS_WORKING_DIR not set; cannot resolve %r", path)
        return (
            "Error: viewer is misconfigured (OCTOPUS_WORKING_DIR missing). "
            "Tell the user to report this — there's nothing you can fix."
        )

    try:
        resolved = resolve_safe_path(working_dir, path)
    except FileViewerError as e:
        # Return as a normal string result (not an exception) so the
        # model sees it as actionable feedback and can try again with
        # a corrected path.
        return f"Could not open {path!r}: {e}"

    logger.info("show_file: %s (%s, %d bytes)", resolved.relative_path, resolved.kind, resolved.size)
    return (
        f"Opened {resolved.relative_path} ({resolved.kind}, {resolved.size} bytes) "
        "in the viewer. The user can see it now."
    )


if __name__ == "__main__":
    # FastMCP.run() defaults to stdio transport — exactly what
    # claude's `--mcp-config` expects.
    mcp.run()
