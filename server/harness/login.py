"""One login contract per harness, behind `profile.login`.

Claude logs in via an OAuth *redirect* (authorize URL → user pastes the
returned code); Codex via a *device code* (verification URL + user code →
poll until the CLI writes auth.json). The flows are genuinely different —
different endpoints, different UX — so this is not a forced single-shape
"begin/poll" abstraction. It's the thin surface the credentials router
needs so it resolves login via `get_harness(backend).login` instead of
importing the managers directly and branching on backend kind:

  - `start(label)`         begin a login; returns the manager's session
  - `submit_code(id,code)` oauth_redirect only — exchange the pasted code
  - `get(id)`              device_code only — poll the in-flight login
  - `cancel(id)`           abort (idempotent)
  - `cleanup_credential(id)` revoke local state on delete (Codex: rmtree
                           its CODEX_HOME; Claude: nothing on disk)

Methods a given method doesn't use raise NotImplementedError. The drivers
wrap the existing `OAuthLoginManager` / `CodexLoginManager` singletons
(which keep their state + tests) — this layer only routes through the
harness and owns the per-kind delete cleanup.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Protocol, runtime_checkable


class LoginMethod(str, Enum):
    """How a harness's interactive login presents itself to the user. The
    frontend renders the matching UI from this declared attribute."""

    oauth_redirect = "oauth_redirect"  # authorize URL → user pastes a code
    device_code = "device_code"        # verification URL + user code → poll


@runtime_checkable
class LoginDriver(Protocol):
    method: LoginMethod

    async def start(self, label: str | None = None) -> Any: ...
    async def submit_code(self, login_id: str, code: str) -> Any: ...
    def get(self, login_id: str) -> Any: ...
    async def cancel(self, login_id: str) -> None: ...
    def cleanup_credential(self, credential_id: str) -> None: ...
