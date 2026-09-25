#!/bin/bash
# Warn — never block — when a commit changes server/ but not the architecture
# doc. architecture.md went three months stale this way: every feature
# documented its own design in docs/plans/ and nothing prompted promoting the
# durable parts upward, so the Applications subsystem (three modules, 1857
# lines) had no architectural description at all.
#
# A nag rather than a gate on purpose: plenty of server changes genuinely do
# not affect architecture, and a false gate teaches people to bypass hooks.

set -uo pipefail
cd "$(dirname "$0")/.."

staged="$(git diff --cached --name-only)"
echo "$staged" | grep -q '^server/' || exit 0
echo "$staged" | grep -q '^docs/architecture.md$' && exit 0

cat >&2 <<'MSG'

  note: this commit touches server/ but not docs/architecture.md.
        If it changed how a subsystem works — rather than just how it is
        implemented — promote that into docs/architecture.md now. That is
        the step whose absence let the doc go three months stale.
        (This is a reminder, not a failure. Commit proceeds.)

MSG
exit 0
