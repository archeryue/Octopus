"""LocalRuntime — a tenant's work runs as a child process on this box.

The default provider, and byte-for-byte the behaviour Octopus had before the
interface existed (runtime-providers.md §1): every spawn site's flags map onto
`ExecSpec` one to one, the streams handed back are asyncio's own, and the
process-group signalling is the code `harness/run.py` always used. It isolates
nothing — accounts share this box at isolation level A (multi-tenancy.md §2).
"""

from __future__ import annotations

import asyncio
import os
import socket
from typing import Any

from ..config import settings
from ..workspace import UserPaths, confine, paths_for
from .base import ExecSpec, Exposed, TenantId

_STDIN = {
    "devnull": asyncio.subprocess.DEVNULL,
    "pipe": asyncio.subprocess.PIPE,
    "inherit": None,
}
_STDERR = {
    "pipe": asyncio.subprocess.PIPE,
    "stdout": asyncio.subprocess.STDOUT,
}


def terminate_process_group(proc: asyncio.subprocess.Process, sig: int) -> bool:
    """Signal the whole process group led by `proc` (so nested children die
    with it), falling back to the direct child if the group can't be resolved.
    Returns True if a group signal was sent. Idempotent / best-effort —
    swallows the races where the process already exited (turn-safety.md §2)."""
    if proc.returncode is not None:
        return False
    try:
        pgid = os.getpgid(proc.pid)
    except (ProcessLookupError, PermissionError):
        pgid = None
    if pgid is not None:
        try:
            os.killpg(pgid, sig)
            return True
        except (ProcessLookupError, PermissionError):
            return False
    try:
        proc.send_signal(sig)
    except (ProcessLookupError, PermissionError):
        pass
    return False


class LocalExecution:
    """An `asyncio.subprocess.Process`, as the `Execution` protocol.

    The streams are the process's own `StreamWriter`/`StreamReader`, so a
    caller reads and writes exactly what it did before. `pid` is local-only —
    not on the protocol — and exists for the diagnostics and tests that inspect
    a real local process; nothing that must work under another provider may
    rely on it.
    """

    def __init__(self, process: asyncio.subprocess.Process, spec: ExecSpec) -> None:
        self.process = process
        self.spec = spec
        self.stdin = process.stdin
        self.stdout = process.stdout
        self.stderr = process.stderr

    @property
    def pid(self) -> int:
        return self.process.pid

    @property
    def returncode(self) -> int | None:
        return self.process.returncode

    async def wait(self) -> int:
        return await self.process.wait()

    async def communicate(self) -> tuple[bytes, bytes]:
        out, err = await self.process.communicate()
        return out or b"", err or b""

    def signal(self, sig: int, *, group: bool = True) -> bool:
        if group:
            return terminate_process_group(self.process, sig)
        if self.process.returncode is not None:
            return False
        try:
            self.process.send_signal(sig)
        except (ProcessLookupError, PermissionError):
            pass
        return False

    def __repr__(self) -> str:
        return f"<LocalExecution pid={self.process.pid} rc={self.process.returncode}>"


def _free_port() -> int:
    """Ask the OS for an unused port, then hand the number to the child.

    There is an unavoidable gap between closing this socket and the child
    binding it. Binding in the parent and passing the fd would close that gap
    but would force every backend to accept an inherited descriptor — a much
    worse contract for a shell script. A collision here is rare, and shows up
    as a start failure with the child's own "address in use" in the log.

    Correct only because the server and the child share a network namespace —
    which is why allocation is the runtime's job (runtime-providers.md §6).
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class LocalRuntime:
    """Every tenant's work as a child of this process, on this box."""

    name = "local"

    async def ensure(self, tenant: TenantId) -> None:
        """The box is already there."""

    async def release(self, tenant: TenantId) -> None:
        """Nothing to put away."""

    async def exec(self, spec: ExecSpec) -> LocalExecution:
        kwargs: dict[str, Any] = {
            "stdin": _STDIN[spec.stdin],
            "stdout": asyncio.subprocess.PIPE,
            "stderr": _STDERR[spec.stderr],
            "env": dict(spec.env),
            "start_new_session": spec.process_group,
        }
        if spec.cwd is not None:
            kwargs["cwd"] = spec.cwd
        if spec.line_limit is not None:
            kwargs["limit"] = spec.line_limit
        process = await asyncio.create_subprocess_exec(*spec.argv, **kwargs)
        return LocalExecution(process, spec)

    async def expose(self, tenant: TenantId) -> Exposed:
        port = _free_port()
        return Exposed(port=port, dial=f"127.0.0.1:{port}")

    def callback_base(self, tenant: TenantId) -> str:
        return f"http://127.0.0.1:{settings.port}"

    def paths(self, tenant: TenantId) -> UserPaths:
        return paths_for(tenant)

    def confine(
        self,
        requested: str | None,
        tenant: TenantId,
        *,
        extra_roots: list[str] | None = None,
    ) -> str:
        return confine(requested, user_id=tenant, extra_roots=extra_roots)
