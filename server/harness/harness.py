"""`Harness` — the single front door for all model/runtime interaction.

One concrete class, parameterized by a `RuntimeProfile`. Feature code goes
through it for everything: creating per-turn runs, lean one-shot model
calls, login, and transcript export/import. Per-framework behavior is the
profile's data + collaborators — there are no harness subclasses.

Capabilities are *derived* from what the profile provides (e.g.
`can_export` ⇐ a transcript codec is present), not a hand-maintained flag
matrix.
"""

from __future__ import annotations

import asyncio
import logging

from .events import HarnessOneshotError
from .login import LoginDriver
from .profile import OneShotContext, RuntimeProfile
from .run import HarnessRun, RunConfig, _which_with_fallback, prepare_spawn

logger = logging.getLogger(__name__)


class Harness:
    """Kind-level adapter for one model runtime. Stateless; one instance per
    backend lives in the registry."""

    def __init__(self, profile: RuntimeProfile) -> None:
        self.profile = profile

    @property
    def backend(self) -> str:
        return self.profile.backend

    # ------------------------------------------------------------------ availability

    def is_available(self) -> bool:
        """Whether this harness's CLI is resolvable on this host."""
        return _which_with_fallback(self.profile.binary) is not None

    # ------------------------------------------------------------------ turns

    def create_run(self, config: RunConfig | None = None) -> HarnessRun:
        """Build the per-turn streaming run engine for this harness."""
        return HarnessRun(self.profile, config)

    @property
    def premature_exit_recovery(self) -> bool:
        """Whether the session_manager run loop should apply the Claude-CLI
        premature-exit respawn (internal quirk, not a product capability)."""
        return self.profile.premature_exit_recovery

    # ------------------------------------------------------------------ derived predicates

    @property
    def can_export(self) -> bool:
        return self.profile.transcript_codec is not None

    @property
    def can_import(self) -> bool:
        return self.profile.transcript_codec is not None

    @property
    def login(self) -> LoginDriver | None:
        return self.profile.login

    @property
    def transcript_codec(self):
        return self.profile.transcript_codec

    # ------------------------------------------------------------------ one-shot

    async def run_oneshot(self, ctx: OneShotContext, *, timeout: float = 90.0) -> str:
        """A lean, non-interactive, tool-free single model call. No MCP, no
        Octopus-tools blurb, no connectors — distinct from a full turn run.
        Returns the model's text. Raises `HarnessOneshotError` (with a stable
        `.code`) on failure; the caller maps it to a domain message.

        The profile builds the argv (applying the credential its own way)
        and extracts the result text from stdout."""
        argv, kwargs = self.profile.build_oneshot_argv(ctx)
        try:
            argv, kwargs = prepare_spawn(argv, kwargs)
            proc = await asyncio.create_subprocess_exec(
                *argv,
                # DEVNULL so a CLI that reads stdin (codex exec) gets EOF
                # immediately and proceeds with the argv prompt instead of
                # blocking forever; harmless for `claude --print`.
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **kwargs,
            )
        except FileNotFoundError:
            raise HarnessOneshotError("not_found", f"{self.profile.binary} CLI not found")
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            raise HarnessOneshotError("timeout", "one-shot call timed out")
        if proc.returncode != 0:
            logger.warning(
                "%s one-shot exited %s: %s",
                self.profile.backend,
                proc.returncode,
                err.decode(errors="replace")[:300],
            )
            raise HarnessOneshotError("failed", "one-shot call failed")
        text = self.profile.parse_oneshot_stdout(out.decode(errors="replace"))
        if not text or not text.strip():
            raise HarnessOneshotError("empty", "one-shot returned an empty response")
        return text
