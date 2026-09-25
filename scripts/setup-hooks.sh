#!/bin/bash
# Install the lefthook git hooks for this repo. Idempotent.
#
# Usage: ./scripts/setup-hooks.sh

set -euo pipefail

cd "$(dirname "$0")/.."

if ! command -v lefthook >/dev/null 2>&1; then
  echo "lefthook not on PATH. Install with one of:"
  echo "  brew install lefthook"
  echo "  npm install -g lefthook"
  echo "  go install github.com/evilmartians/lefthook@latest"
  exit 1
fi

lefthook install

echo "Hooks installed. From now on, commits will run:"
echo "  - eslint + tsc --noEmit + vitest   (when web TS changes)"
echo "  - ruff + mypy + pytest -m 'not real'  (when Python changes)"
echo "  - contracts.ts drift check   (when routes or pydantic models change)"
echo
echo "To run all of them at once, regardless of what changed:"
echo "  ./scripts/check.sh            (add --e2e for Playwright)"
