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
import signal

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

    def is_auth_error(self, text: str) -> bool:
        """Whether `text` (a failed turn's combined error output + stderr)
        looks like this backend's auth-credential rejection — a 401 / expired
        or revoked token (harness-credential-reauth.md §3). Case-insensitive
        substring match over the profile's `auth_error_patterns`; pure, no I/O.
        Callers gate on the turn actually having failed so a tool that merely
        returns a 401 to the model can't trip it."""
        if not text or not self.profile.auth_error_patterns:
            return False
        low = text.lower()
        return any(p in low for p in self.profile.auth_error_patterns)

    def is_transient_error(self, text: str) -> bool:
        """Whether `text` (a failed turn's combined error output + stderr) looks
        like a TRANSIENT provider-reliability failure — a 5xx / overloaded /
        dropped-connection / timeout that a bounded retry can ride out
        (harness-transient-retry.md §3). Case-insensitive substring match over
        the profile's `transient_error_patterns`; pure. Mutually exclusive with
        `is_auth_error`, and excludes quota/credit (never retried). Callers gate
        on the turn having failed with no output yet."""
        if not text or not self.profile.transient_error_patterns:
            return False
        low = text.lower()
        return any(p in low for p in self.profile.transient_error_patterns)

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

    # ------------------------------------------------------------------ fork

    @property
    def can_fork(self) -> bool:
        return self.profile.can_fork

    async def prepare_fork(
        self,
        messages: list,
        working_dir: str,
        resume_id_hint: str | None,
        fork_id: str,
    ):
        """Prepare backend-specific state so a new fork session can spawn at the
        branch point (session-tree-rewind.md §3.1). Returns a `ForkArtifact`.

        NATIVE_TRANSCRIPT (Claude) synthesizes a resumable transcript on disk
        named by `resume_id_hint`; HISTORY_REPLAY (Codex) returns
        `needs_replay=True` and does no on-disk work. Raises
        `BackendForkNotSupported` only when the backend has no strategy."""
        from .fork import BackendForkNotSupported

        if not self.profile.can_fork or self.profile.fork_prepare is None:
            raise BackendForkNotSupported(self.backend)
        return await self.profile.fork_prepare(
            messages, working_dir, resume_id_hint, fork_id
        )

    async def prepare_fork_copy(
        self,
        *,
        parent_working_dir: str,
        parent_resume_id: str | None,
        parent_credential=None,
        dest_working_dir: str,
        new_resume_id: str,
    ):
        """Full-copy fork (session-fork-copy.md): copy the backend's NATIVE
        transcript (the parent's real conversation, identified by
        `parent_resume_id`) into `new_resume_id` at `dest_working_dir`, so a
        `/fork` duplicate resumes with real context — no history replay into the
        first prompt. Returns a `ForkArtifact`. Falls back to
        `needs_replay=True` when the backend has no native-copy strategy or the
        parent has no transcript yet (e.g. it never ran a turn)."""
        from .fork import BackendForkNotSupported, ForkArtifact

        if not self.profile.can_fork:
            raise BackendForkNotSupported(self.backend)
        if self.profile.fork_copy is None:
            return ForkArtifact(resume_id=None, needs_replay=True)
        return await self.profile.fork_copy(
            parent_working_dir=parent_working_dir,
            parent_resume_id=parent_resume_id,
            parent_credential=parent_credential,
            dest_working_dir=dest_working_dir,
            new_resume_id=new_resume_id,
        )

    async def cleanup_incomplete_fork_artifacts(
        self,
        working_dir: str,
        resume_id_hint: str | None,
        fork_id: str,
        *,
        credential=None,
    ) -> None:
        """Sweep any backend-specific files a fork strategy may have left when a
        saga didn't complete (session-tree-rewind.md §3.1, session-fork-copy.md).
        Idempotent — safe on every boot for every 'initializing' row. No-op when
        the backend has no on-disk fork artifacts to sweep. `credential` lets a
        directory-backed backend (Codex / CODEX_HOME) locate its rollout store."""
        if self.profile.fork_cleanup is None:
            return
        await self.profile.fork_cleanup(
            working_dir, resume_id_hint, fork_id, credential=credential
        )

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
        from .run import _terminate_process_group

        def _reap() -> None:
            # Kill the whole group (run_oneshot is a session leader via
            # prepare_spawn) so nothing lingers. turn-safety.md §2.
            _terminate_process_group(proc, signal.SIGKILL)

        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            _reap()
            try:
                await proc.wait()  # reap the killed leader (no zombie)
            except Exception:
                logger.debug("run_oneshot proc.wait() after kill failed", exc_info=True)
            raise HarnessOneshotError("timeout", "one-shot call timed out")
        except asyncio.CancelledError:
            # Job cancelled (or the job-level wait_for expired) while we were in
            # communicate() — reap the group too, else the CLI orphans (Vera
            # review). Best-effort wait, then propagate the cancellation.
            _reap()
            try:
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except Exception:
                pass
            raise
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
