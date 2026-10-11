"""Which Runtime this server runs on, and the lifecycle around it.

`get_runtime()` is what every spawn site calls. It returns the configured
provider wrapped in `ManagedRuntime`, which owns the two decisions
runtime-providers.md §3 gives the *server* rather than the provider:

* `ensure(tenant)` before every `exec` — idempotent, so it is always called;
* `release(tenant)` once the tenant has nothing left running, after a grace
  period, cancelled if anything starts in the meantime.

"Nothing left running" is counted, not inferred: every execution the wrapper
hands out is live until its `wait()` or `communicate()` returns. Every spawn
site already awaits its process — the turn engine on stop, a one-shot on
`communicate`, a background task and an Application backend on exit — so the
count is exactly the set of the tenant's processes, held CLIs included. An
execution nobody awaits keeps its tenant from being released, which is the
safe way to be wrong: a `release()` with something still running is a server
bug the contract does not ask a provider to absorb.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
from typing import Any

from ..config import settings
from ..workspace import UserPaths
from .base import ExecSpec, Execution, Exposed, Runtime, TenantId
from .local import LocalRuntime

logger = logging.getLogger(__name__)


def load_provider(name: str) -> Runtime:
    """`local`, or `package.module:Class` — a provider installed alongside
    Octopus and constructed with no arguments (it reads its own settings)."""
    if name == "local":
        return LocalRuntime()
    module_name, sep, attr = name.partition(":")
    if not sep or not module_name or not attr:
        raise ValueError(
            f"runtime_provider must be 'local' or 'package.module:Class', got {name!r}"
        )
    cls = getattr(importlib.import_module(module_name), attr)
    return cls()


class ManagedExecution:
    """An execution that tells its runtime when it is over.

    Transparent otherwise: the streams are the provider's own objects, and any
    attribute the protocol does not name (LocalExecution's `pid`) is passed
    through, so a caller that held the provider's execution sees no change.
    """

    def __init__(self, inner: Execution, on_done: Any) -> None:
        self._inner = inner
        self._on_done = on_done
        self.stdin = inner.stdin
        self.stdout = inner.stdout
        self.stderr = inner.stderr

    @property
    def inner(self) -> Execution:
        return self._inner

    @property
    def returncode(self) -> int | None:
        return self._inner.returncode

    def _finish(self) -> None:
        if self._on_done is not None:
            on_done, self._on_done = self._on_done, None
            on_done()

    async def wait(self) -> int:
        rc = await self._inner.wait()
        self._finish()
        return rc

    async def communicate(self) -> tuple[bytes, bytes]:
        result = await self._inner.communicate()
        self._finish()
        return result

    def signal(self, sig: int, *, group: bool = True) -> bool:
        return self._inner.signal(sig, group=group)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def __repr__(self) -> str:
        return f"<ManagedExecution {self._inner!r}>"


class ManagedRuntime:
    """The configured provider, with `ensure` / `release` decided here."""

    def __init__(self, provider: Runtime, *, release_grace: float | None = None) -> None:
        self.provider = provider
        self.name = provider.name
        self._release_grace = release_grace
        self._live: dict[TenantId, int] = {}
        self._pending_release: dict[TenantId, asyncio.TimerHandle] = {}
        self._release_tasks: set[asyncio.Task[None]] = set()

    @property
    def release_grace(self) -> float:
        if self._release_grace is not None:
            return self._release_grace
        return settings.runtime_release_grace_seconds

    def live(self, tenant: TenantId) -> int:
        """How many of `tenant`'s executions have not been awaited to the end."""
        return self._live.get(tenant, 0)

    # ------------------------------------------------------------ lifecycle

    async def ensure(self, tenant: TenantId) -> None:
        self._cancel_release(tenant)
        await self.provider.ensure(tenant)

    async def release(self, tenant: TenantId) -> None:
        self._cancel_release(tenant)
        await self.provider.release(tenant)

    def _cancel_release(self, tenant: TenantId) -> None:
        handle = self._pending_release.pop(tenant, None)
        if handle is not None:
            handle.cancel()

    def _execution_done(self, tenant: TenantId) -> None:
        remaining = self._live.get(tenant, 0) - 1
        if remaining > 0:
            self._live[tenant] = remaining
            return
        self._live.pop(tenant, None)
        self._schedule_release(tenant)

    def _schedule_release(self, tenant: TenantId) -> None:
        self._cancel_release(tenant)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._pending_release[tenant] = loop.call_later(
            self.release_grace, self._release_if_idle, tenant
        )

    def _release_if_idle(self, tenant: TenantId) -> None:
        self._pending_release.pop(tenant, None)
        if self.live(tenant):
            return
        task = asyncio.get_running_loop().create_task(
            self._release_quietly(tenant), name=f"runtime-release-{tenant}"
        )
        self._release_tasks.add(task)
        task.add_done_callback(self._release_tasks.discard)

    async def _release_quietly(self, tenant: TenantId) -> None:
        if self.live(tenant):
            return
        try:
            await self.provider.release(tenant)
        except Exception:
            logger.exception("runtime %s: release(%s) failed", self.name, tenant)

    async def shutdown(self) -> None:
        """Drop pending releases; let any in flight finish. Runtimes are not
        released on shutdown — a restart is not the tenant going idle, and the
        processes they hold are being stopped by their own owners."""
        for handle in self._pending_release.values():
            handle.cancel()
        self._pending_release.clear()
        if self._release_tasks:
            await asyncio.gather(*self._release_tasks, return_exceptions=True)

    # ------------------------------------------------------------ the rest

    async def exec(self, spec: ExecSpec) -> ManagedExecution:
        await self.ensure(spec.tenant)
        inner = await self.provider.exec(spec)
        self._live[spec.tenant] = self._live.get(spec.tenant, 0) + 1
        return ManagedExecution(inner, lambda: self._execution_done(spec.tenant))

    async def expose(self, tenant: TenantId) -> Exposed:
        return await self.provider.expose(tenant)

    def callback_base(self, tenant: TenantId) -> str:
        return self.provider.callback_base(tenant)

    def paths(self, tenant: TenantId) -> UserPaths:
        return self.provider.paths(tenant)

    def confine(
        self,
        requested: str | None,
        tenant: TenantId,
        *,
        extra_roots: list[str] | None = None,
    ) -> str:
        return self.provider.confine(requested, tenant, extra_roots=extra_roots)


_runtime: ManagedRuntime | None = None


def get_runtime() -> ManagedRuntime:
    """The server's runtime, built from `settings.runtime_provider` on first
    use."""
    global _runtime
    if _runtime is None:
        _runtime = ManagedRuntime(load_provider(settings.runtime_provider))
    return _runtime


def set_runtime(runtime: Runtime | ManagedRuntime | None) -> ManagedRuntime | None:
    """Install `runtime` (wrapped if it is a bare provider), or reset to the
    configured one with None. Returns what was installed before. For tests and
    for an embedder that constructs its provider itself."""
    global _runtime
    previous = _runtime
    if runtime is None or isinstance(runtime, ManagedRuntime):
        _runtime = runtime
    else:
        _runtime = ManagedRuntime(runtime)
    return previous
