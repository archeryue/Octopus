#!/usr/bin/env python3
"""Measure Octopus's own process footprint — the before/after for B1.

B1 (docs/plans/polish-2026-09.md §4) moves the per-session MCP stdio
sidecars onto streamable-HTTP served from the main app, taking the sidecar
count to zero. The claim is ~39 MB PSS per sidecar, seven per session. This
script is how that claim gets checked rather than quoted.

    ./scripts/measure-footprint.py            # human-readable
    ./scripts/measure-footprint.py --json     # one JSON line, for diffing

Two details this gets right, because getting them wrong is how the original
hand measurement went astray:

1. **PSS, not RSS.** RSS counts shared pages once per process, so seven
   Python interpreters sharing libpython look far heavier than they are.
   `/proc/<pid>/smaps_rollup` reports proportional set size, which splits
   shared pages across the processes mapping them. On this codebase RSS
   overstates the sidecar total by roughly 25%.

2. **argv-exact matching.** Every `claude --print` subprocess carries the
   whole `--mcp-config` JSON on its command line, and that JSON contains the
   string "server.mcp_servers" seven times. A substring match over `ps`
   output therefore counts the CLI processes as sidecars and inflates the
   total by hundreds of MB. This matches on argv structure instead:
   argv[1] == "-m" and argv[2] starts with "server.mcp_servers".

This script is scaffolding with a stated demolition date: once §9 G's
periodic sampler lands it collects the same numbers continuously, and this
file should be deleted rather than maintained.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

PROC = Path("/proc")


def _argv(pid: str) -> list[str]:
    """argv as a list of strings, or [] if the process is gone/unreadable."""
    try:
        raw = (PROC / pid / "cmdline").read_bytes()
    except (OSError, PermissionError):
        return []
    return [a.decode("utf-8", "replace") for a in raw.split(b"\0") if a]


def _pss_kb(pid: str) -> int | None:
    """Proportional set size in KB, or None if unreadable."""
    try:
        for line in (PROC / pid / "smaps_rollup").read_text().splitlines():
            if line.startswith("Pss:"):
                return int(line.split()[1])
    except (OSError, PermissionError, ValueError, IndexError):
        return None
    return None


def _classify(argv: list[str]) -> tuple[str, str] | None:
    """Bucket a process, or None if it isn't ours.

    Order matters: the sidecar test is argv-structural, so it cannot be
    confused by a `claude` process quoting server.mcp_servers in its config.
    """
    if not argv:
        return None
    exe = argv[0]

    # python3 -m server.mcp_servers.<name>
    if len(argv) >= 3 and argv[1] == "-m" and argv[2].startswith("server.mcp_servers"):
        return ("sidecar", argv[2].replace("server.mcp_servers.", ""))

    if exe.endswith("/octopus") or (len(argv) >= 2 and argv[1].endswith("/octopus")):
        return ("server", "octopus serve")

    # An engine subprocess: `claude --print ...` or `codex exec ...`. Match on
    # the subcommand/flag so an interactive CLI the developer is using by hand
    # isn't counted as part of Octopus's footprint.
    base = exe.rsplit("/", 1)[-1]
    if base in ("claude", "codex") and ("--print" in argv or "exec" in argv[1:3]):
        return ("engine", base)
    return None


def collect() -> dict:
    buckets: dict[str, list[dict]] = {"sidecar": [], "server": [], "engine": []}
    for entry in PROC.iterdir():
        if not entry.name.isdigit():
            continue
        argv = _argv(entry.name)
        hit = _classify(argv)
        if hit is None:
            continue
        kind, label = hit
        pss = _pss_kb(entry.name)
        if pss is None:
            continue
        buckets[kind].append({"pid": int(entry.name), "label": label, "pss_kb": pss})

    sidecars = buckets["sidecar"]
    distinct = sorted({s["label"] for s in sidecars})
    per_session = len(distinct) or 1
    sessions = round(len(sidecars) / per_session, 1) if sidecars else 0

    return {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "sidecars": {
            "count": len(sidecars),
            "namespaces": distinct,
            "per_session": per_session,
            "implied_sessions": sessions,
            "total_pss_mb": round(sum(s["pss_kb"] for s in sidecars) / 1024, 1),
            "mean_pss_mb": round(
                sum(s["pss_kb"] for s in sidecars) / len(sidecars) / 1024, 1
            )
            if sidecars
            else 0.0,
        },
        "server_pss_mb": round(sum(p["pss_kb"] for p in buckets["server"]) / 1024, 1),
        "engines": {
            "count": len(buckets["engine"]),
            "total_pss_mb": round(
                sum(p["pss_kb"] for p in buckets["engine"]) / 1024, 1
            ),
        },
        "detail": buckets,
    }


def measure_startup(repo: Path, n: int = 3) -> dict:
    """Cost of importing one sidecar module — the per-sidecar share of
    session-start latency. A component measure, not end-to-end session start:
    it isolates interpreter + import cost, which is what B1 removes."""
    py = repo / ".venv/bin/python3"
    if not py.exists():
        return {"available": False}
    best = None
    for _ in range(n):
        t0 = time.perf_counter()
        r = subprocess.run(
            [str(py), "-c", "import server.mcp_servers.bg"],
            cwd=repo, capture_output=True,
        )
        dt = time.perf_counter() - t0
        if r.returncode == 0:
            best = dt if best is None else min(best, dt)
    if best is None:
        return {"available": False}
    return {
        "available": True,
        "import_ms": round(best * 1000, 1),
        "per_session_ms": round(best * 1000 * 7, 1),
    }


def main() -> int:
    repo = Path(__file__).resolve().parent.parent
    snap = collect()
    snap["startup"] = measure_startup(repo)

    if "--json" in sys.argv:
        snap.pop("detail")
        print(json.dumps(snap))
        return 0

    sc = snap["sidecars"]
    print(f"Octopus footprint — {snap['ts']}  (PSS, shared pages not double-counted)")
    print()
    print(f"  MCP sidecars      {sc['count']:>4} procs   {sc['total_pss_mb']:>7.1f} MB"
          f"   (mean {sc['mean_pss_mb']} MB)")
    if sc["namespaces"]:
        print(f"    namespaces      {sc['per_session']} per session: {', '.join(sc['namespaces'])}")
        print(f"    implies         ~{sc['implied_sessions']} active session(s)")
    print(f"  octopus serve     {len(snap['detail']['server']):>4} proc    {snap['server_pss_mb']:>7.1f} MB")
    print(f"  engine CLIs       {snap['engines']['count']:>4} procs   {snap['engines']['total_pss_mb']:>7.1f} MB")
    print()
    own = round(sc["total_pss_mb"] + snap["server_pss_mb"], 1)
    if own:
        share = round(100 * sc["total_pss_mb"] / own)
        print(f"  Octopus's own total: {own} MB, of which sidecars are {share}%")
    st = snap["startup"]
    if st["available"]:
        print(f"  sidecar import cost: {st['import_ms']} ms  "
              f"(x7 per session = {st['per_session_ms']} ms)")
    print()
    print("  B1 target: sidecar count 0. Re-run after the change and compare.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
