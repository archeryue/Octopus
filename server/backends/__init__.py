"""Backend abstraction: spawn a CLI subprocess, read JSONL, emit normalized events.

See `docs/future-features.md` #1 for the design and `docs/cli-protocol-notes.md`
for the wire protocol this is built against.
"""

from .base import BackendBase, BackendCredential, BackendEvent
from .claude_code import ClaudeCodeBackend
from .subprocess_jsonl import SubprocessJsonlBackend

__all__ = [
    "BackendBase",
    "BackendCredential",
    "BackendEvent",
    "ClaudeCodeBackend",
    "SubprocessJsonlBackend",
]
