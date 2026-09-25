#!/bin/bash
# Fail if web/src/api/contracts.ts has drifted from the FastAPI OpenAPI schema.
#
# contracts.ts is generated (`cd web && bun run generate:contracts`), but
# nothing enforces regeneration — so a route or pydantic change can silently
# leave the frontend compiling against stale types. Shared by
# scripts/check.sh and the lefthook pre-commit hook.
#
# Generates into a temp dir and diffs. `bun run generate:contracts` writes
# src/api/contracts.ts in place, so invoking it here would mean a *check*
# mutated a tracked file.

set -uo pipefail

cd "$(dirname "$0")/.."
ROOT="$PWD"

if [ -d "$HOME/.nvm/versions/node" ]; then
  export PATH="$HOME/.nvm/versions/node/$(ls "$HOME/.nvm/versions/node" | sort -V | tail -1)/bin:$PATH"
fi

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

if ! "$ROOT/.venv/bin/python" "$ROOT/scripts/dump-openapi.py" > "$tmp/openapi.json"; then
  echo "could not dump the OpenAPI schema (does server.main import cleanly?)" >&2
  exit 1
fi

if ! (cd "$ROOT/web" && npx openapi-typescript "$tmp/openapi.json" -o "$tmp/contracts.ts" >/dev/null 2>&1); then
  echo "openapi-typescript failed — is web/node_modules installed?" >&2
  exit 1
fi

if diff -q "$ROOT/web/src/api/contracts.ts" "$tmp/contracts.ts" >/dev/null 2>&1; then
  echo "contracts.ts is in sync with the OpenAPI schema"
  exit 0
fi

cat >&2 <<MSG
contracts.ts is STALE — the OpenAPI schema and the committed types disagree.

Regenerate with:
    cd web && bun run generate:contracts

Diff (committed vs freshly generated), first 40 lines:
MSG
diff "$ROOT/web/src/api/contracts.ts" "$tmp/contracts.ts" | head -40 >&2
exit 1
