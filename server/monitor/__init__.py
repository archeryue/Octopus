"""Monitoring: what the system actually did, kept long enough to analyse.

The motivating case is in this repo's own history. The Gmail connector was
unavailable for eleven consecutive days; a daily schedule fired on each of
them, failed on each of them, and nothing anywhere counted it. The mechanism to
record a broken connector already existed (`mark_needs_reconnect`); what was
missing was any durable record of repeated failure.

Design (docs/plans/polish-2026-09.md §9):

- A bounded, non-blocking sink (`store.MetricsStore`) in front of a batched
  writer, so recording cannot stall or fail a turn.
- A separate `octopus-metrics.db`, independently prunable, 30-day retention.
- Hooks at chokepoints that already exist, so instrumentation does not spread
  through feature code. In particular turn metrics ride `HarnessEvent`, which
  already carries `cost`, `duration_ms` and `num_turns` and is already
  backend-neutral — so nothing backend-specific is needed to measure a turn,
  which is what keeps this on the right side of the harness contract.
"""

from __future__ import annotations

from .store import Event, MetricsStore, Sample

__all__ = ["Event", "MetricsStore", "Sample", "monitor", "record", "sample"]

# One process-wide store, initialized from main's lifespan. A module-level
# singleton matches how `session_manager` and `bg_task_manager` are reached, and
# means a hook site is one import rather than a parameter threaded through
# layers that have no other reason to know about metrics.
monitor: MetricsStore | None = None


def set_store(store: MetricsStore | None) -> None:
    global monitor
    monitor = store


def record(event: Event) -> None:
    """Record an event if monitoring is up; otherwise do nothing.

    The no-op path matters: tests, the CLI and `octopus handoff` all import
    feature code without a running server, and a hook must not care.
    """
    if monitor is not None:
        monitor.record(event)


def sample(s: Sample) -> None:
    if monitor is not None:
        monitor.sample(s)
