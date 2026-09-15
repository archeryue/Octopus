"""Shared stand-in base for `HarnessRun` fakes.

The run loop asks a backend whether its process can be kept alive between
turns (inline-steering.md §7). A fake that doesn't answer crashes the turn
with an `AttributeError` instead of failing the assertion under test, and the
failure points at session_manager rather than at the fake — so the contract
lives here, once, for every test that stands in for a run.

Default is non-reusable: a fake behaves like the spawn-per-turn path unless a
test explicitly opts in.
"""

from __future__ import annotations


class FakeRunBase:
    """The parts of `HarnessRun` every stand-in must answer."""

    reusable = False

    def is_alive(self) -> bool:
        return True

    def spawn_signature(self, working_dir, credential) -> str:
        return "fake-signature"

    async def send_turn(self, prompt: str) -> None:
        self.sent_turns = getattr(self, "sent_turns", [])
        self.sent_turns.append(prompt)
