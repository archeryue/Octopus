"""Backwards-compatible re-export of the persistence layer.

The implementation moved to `server/db/` — one file per subject rather than
one 2,600-line class (docs/plans/polish-2026-09.md §3 A4). This module stays
so that `from .database import Database`, which appears throughout the codebase
and the tests, keeps working: the split was meant to make the code readable,
not to make 40 call sites churn.
"""

from __future__ import annotations

from .db import Database, DatabaseBase
from .db.schema import _SCHEMA

__all__ = ["Database", "DatabaseBase", "_SCHEMA"]
