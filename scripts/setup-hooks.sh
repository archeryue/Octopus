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
echo "  - tsc --noEmit (when web/ TS changes)"
echo "  - pytest tests/ (when server/ or tests/ Python changes)"
