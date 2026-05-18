#!/usr/bin/env python3
"""Dump the FastAPI app's OpenAPI schema to stdout as JSON.

Used by `bun run generate:contracts` in web/ to regenerate
`web/src/api/contracts.ts` whenever a server route or pydantic model
changes. Run from the repo root:

  .venv/bin/python scripts/dump-openapi.py > openapi.json

Doesn't trigger the FastAPI lifespan (DB init, bridge startup, etc.);
just walks the registered routes.
"""

import json
import sys
from pathlib import Path

# Allow `from server import ...` whether we're invoked from repo root,
# from web/, or anywhere else.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.main import app  # noqa: E402

schema = app.openapi()
json.dump(schema, sys.stdout, indent=2)
sys.stdout.write("\n")
