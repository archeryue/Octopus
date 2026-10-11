"""The Runtime protocol: where a tenant's work executes (runtime-providers.md).

Octopus assumed one thing about execution everywhere: a child process runs on
this box, in a directory this process can `open()`. This module names that
assumption as an interface so a second implementation has somewhere to exist.
`LocalRuntime` (`local.py`) is the default and is exactly today's behaviour;
other providers live outside this repository and are held to the conformance
suite, which is the contract — this file is its vocabulary.

The protocol is versioned surface: a provider ships on its own release
cadence, so a breaking change here needs a new `PROTOCOL_VERSION`, not a
commit.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable

from ..workspace import UserPaths

PROTOCOL_VERSION = "runtime-1.0"

# Whose work this is: an account id, or None for the install itself — the
# pre-accounts era, where nothing has an owner yet (multi-tenancy.md §9).
TenantId = str | None

# Where the child's stdin comes from.
#   devnull — EOF at once (a one-shot must not block reading, `codex exec`)
#   pipe    — the caller writes it (the streaming turn's bidirectional stdin)
#   inherit — whatever this process's stdin is. Kept only because two call
#             sites have always spawned that way; under systemd it is
#             /dev/null, and a remote provider may treat it as `devnull`.
StdinMode = Literal["devnull", "pipe", "inherit"]

# Where the child's stderr goes: its own pipe, or merged into stdout (a log
# pump that wants one ordered stream: install.sh, start.sh, `codex login`).
StderrMode = Literal["pipe", "stdout"]


@dataclass(frozen=True)
class ExecSpec:
    """One process to start in a tenant's runtime."""

    tenant: TenantId
    argv: Sequence[str]
    # The complete environment. A provider adds NOTHING from the server's own
    # environment: for any provider but LocalRuntime that environment is the
    # operator's — the install token, the master-key path, every secret the
    # service was started with. Inheritance is the caller's explicit act
    # (`{**os.environ, ...}`), never the runtime's implicit one.
    env: Mapping[str, str]
    cwd: str | None = None
    stdin: StdinMode = "devnull"
    stderr: StderrMode = "pipe"
    # Own process group (session leader), so the whole tree the child spawns
    # can be signalled as a unit — and, for a detached task, survives its
    # caller. False only where the child must stay in ours (`git`).
    process_group: bool = True
    # Max bytes in one stdout line before the reader raises (asyncio's
    # `limit`); None keeps asyncio's default.
    line_limit: int | None = None
    # Free-form provider hints; LocalRuntime ignores them.
    hints: Mapping[str, str] = field(default_factory=dict)


class AsyncWriter(Protocol):
    def write(self, data: bytes) -> None: ...
    async def drain(self) -> None: ...
    def close(self) -> None: ...
    def is_closing(self) -> bool: ...


class AsyncReader(Protocol):
    """A line-oriented stream: `readline()`/`read()`, and `async for line`."""

    async def readline(self) -> bytes: ...
    async def read(self, n: int = -1) -> bytes: ...
    def __aiter__(self) -> AsyncReader: ...
    async def __anext__(self) -> bytes: ...


@runtime_checkable
class Execution(Protocol):
    """A running process, as a caller may touch it.

    No pid: a pid is meaningless outside the runtime's pid namespace, and every
    use Octopus had for one was "signal this process and its children" —
    `signal(group=True)`. No timeout: callers wrap `wait()`/`communicate()` in
    `asyncio.wait_for`, which is their policy, not the runtime's.
    """

    @property
    def stdin(self) -> AsyncWriter | None:
        """Present when spawned with `stdin="pipe"`."""
        ...

    @property
    def stdout(self) -> AsyncReader | None: ...

    @property
    def stderr(self) -> AsyncReader | None:
        """None when merged into stdout (`stderr="stdout"`)."""
        ...

    @property
    def returncode(self) -> int | None: ...

    async def wait(self) -> int: ...

    async def communicate(self) -> tuple[bytes, bytes]:
        """Read stdout and stderr to EOF concurrently, then wait — the shape
        that cannot deadlock on a full pipe. stderr is b"" when merged."""
        ...

    def signal(self, sig: int, *, group: bool = True) -> bool:
        """Send `sig` to the process — and, with `group`, its whole process
        group. Best-effort and synchronous: it returns once the signal is on
        its way (a remote provider queues it), swallows the race where the
        process has already exited, and returns True only when a group signal
        was sent."""
        ...


@dataclass(frozen=True)
class Exposed:
    """A port a child in the runtime may bind, and how the server reaches it."""

    port: int  # what the child binds, inside the runtime
    dial: str  # `host:port` the server connects to, from where it runs


class Runtime(Protocol):
    """Where a tenant's work runs. One per account, created on demand.

    `ensure` / `release` are the server's decisions, made from facts it
    already has (runtime-providers.md §3): `ensure` before every `exec`,
    `release` once the tenant has nothing left running. Both idempotent.
    """

    name: str

    async def ensure(self, tenant: TenantId) -> None: ...
    async def release(self, tenant: TenantId) -> None: ...

    # Execution — one primitive for every shape Octopus spawns.
    async def exec(self, spec: ExecSpec) -> Execution: ...

    # Network, server → runtime: a port for an Application backend (§6).
    async def expose(self, tenant: TenantId) -> Exposed: ...

    # Network, runtime → server: the base URL work inside this tenant's
    # runtime uses to reach this server — MCP tools, the agent API (§7).
    def callback_base(self, tenant: TenantId) -> str: ...

    # Paths — the tenant's directories as the runtime sees them (§8, §9).
    def paths(self, tenant: TenantId) -> UserPaths: ...
    def confine(
        self,
        requested: str | None,
        tenant: TenantId,
        *,
        extra_roots: list[str] | None = None,
    ) -> str: ...
