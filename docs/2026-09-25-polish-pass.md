# What changed on 2026-09-25

A polish pass over Octopus: nothing here is a new product capability except
the monitor. The rest pays down debts that had accumulated under seven months
of fast feature work — and two of them turned out to be live defects.

Branch `polish-2026-09`, 11 commits, all gates green.
Plan and reasoning: [`plans/polish-2026-09.md`](plans/polish-2026-09.md).

---

## The two real bugs

**Telegram's Allow/Deny buttons silently did nothing.**
`bridges/manager.py` called `approve_tool` / `deny_tool` without awaiting
them. Both are `async def`, so the coroutines were created, never scheduled,
and dropped. The web path (`routers/ws.py`) always awaited correctly, so this
only ever broke on the bridge — a feature the README advertises.

It survived 218 commits because `tests/test_bridge_manager.py` declared those
two methods as plain `def` on its fake. **The test double had drifted from the
real signature, so it agreed with the buggy call site and certified it**, while
`test_bridge_telegram.py` replaces `handle_tool_decision` wholesale with an
`AsyncMock` and never reaches the manager's own two lines. Fixed in all three
places, with three tests confirmed to fail against the reintroduced bug.

Found by mypy on its first run.

**The sidecar gauge counted other people's processes.**
The new sampler counted every `server.mcp_servers.*` process on the host, so an
isolated instance that had spawned nothing reported 12. They belonged to the
production server running beside it. Two instances would each attribute the
other's children to itself, and the gauge answered the wrong question — it is
meant to say "how many did *I* start". It now walks the parent chain.

Found by the browser E2E, which is the only place it could have been found: the
machine has to have a second Octopus on it.

---

## Performance

### The MCP namespaces are served, not spawned (B1)

Seven Python subprocesses started **per session**, ~39 MB PSS each — measured
at 21 procs / 831 MB across three sessions, **~85% of Octopus's own memory** —
plus ~242 ms of import cost, seven times, on the session-start path. They were
thin shims that HTTP-POSTed back to this very app.

They are now mounted at `/mcp/<name>` over streamable-HTTP. **Spawn count: 0**,
confirmed in the browser: `mcp_sidecar_count` reads 0 on the Monitor page.

Two things decided the shape:

- **The config key could not move.** Both CLIs build the tool name from it, so
  `mcp__bg__run` would silently become something else. That ruled out
  consolidating seven servers into one and ruled in per-namespace mounts.
  Connectors mount once per *kind* while their key stays per-installation —
  key and URL are independent, so `mcp__gmail_e255c1__search` keeps its name
  over a shared mount.
- **Identity had to move.** One endpoint serves every session, so the session
  can no longer arrive as `OCTOPUS_SESSION_ID` in a spawn environment. It rides
  a per-session scoped credential — derived, never stored, rotated with the
  access token, following the `app_scope_token` pattern already in the
  codebase. **Codex forced that choice**: it accepts a URL and the *name* of an
  env var to read a bearer from, but not an arbitrary header (its `--env` is
  stdio-only). A header-based scheme would have worked on Claude and been
  impossible on Codex — a capability difference inside the harness, which is
  what `server/harness/` exists to prevent.

A compatibility spike proved all three legs against both real CLIs *before* any
of it was written. Without it, the header approach would have been built and
would have failed on the first Codex session.

### The database stops doing unbounded work (B2–B5)

- `load_messages` gained `max_seq` and `newest_first`. Fork replay used to load
  a whole transcript and drop the tail in Python — on the largest session here
  that is 4,600 rows read to keep ten.
- `append_message` deferred its commit with **nothing bounding it**: three
  explicit flush sites, no timer, and the production WAL sitting at 7 MB as a
  result. Now ~0.5 s or 32 rows, whichever trips first.
- `busy_timeout` and an explicit `synchronous=NORMAL`, so "database is locked"
  waits instead of raising, and the durability story is stated rather than
  inherited.

---

## Correctness

### Migrations that fail now say so (A3)

`_apply_migrations` was 200 lines of `try: ALTER …; except Exception: pass`.
The comment said "column already exists" — SQLite has no `ADD COLUMN IF NOT
EXISTS` — but a bare handler absorbs a misspelled column, a renamed table, a
locked database and a full disk identically. A broken migration silently
no-opped and resurfaced later as an inexplicably missing column.

Not hypothetical: an ordinary `check.sh` run surfaced
`duplicate column name: delegation_request` being absorbed, visible only
because it happened to escape as a GC-time warning.

The 20 additive migrations are now declared as data and applied through one
guarded path that asks `PRAGMA table_info` first, and a `schema_migrations`
ledger records what ran. **Verified against a copy of the real 82.9 MB
production database**, because a unit test on a fresh schema cannot exercise
the partially-migrated case that makes this risky:

    columns lost   NONE
    rows lost      NONE
    messages       46,742 -> 46,742
    second boot    idempotent

---

## Monitoring (G) — the one new capability

**Why it exists:** the Gmail connector was unavailable for **eleven
consecutive days**. A daily schedule fired on each of them, failed on each of
them, and nothing counted it. `mark_needs_reconnect` already existed; the
record of repeated failure did not.

- A bounded, non-blocking sink (drop-oldest, cannot raise) in front of a
  batched writer.
- A **separate** `octopus-metrics.db`, 30-day retention swept by the scheduler
  already in the process. Separate because `octopus.db` is already ~83 MB of
  product data with no retention, and a metrics bug must not be able to
  corrupt session history.
- **Nine hooks**, all at chokepoints that already existed. Turn outcome rides
  `HarnessEvent`, which already carries `duration_ms`/`cost`/`num_turns` for
  both backends — so measuring a turn needs nothing backend-specific, which is
  what keeps this on the right side of the harness contract. HTTP is labelled
  by route *template*, never raw path, so a session id cannot mint a dimension.
- `octopus monitor [turns|errors|connectors|schedules|resources|http]`,
  `/api/monitor/*`, and a page in the app. The data is plain SQLite, so
  anything not anticipated is a `sqlite3` query away.

An empty section reads "Nothing recorded in this window" rather than rendering
blank — a blank panel reads as a clean bill of health, and telling those two
apart is the entire point.

---

## Tooling and hygiene

### ruff + mypy, in front of 28k previously-unchecked lines (F1)

The annotations were thorough — 89% return coverage — and verified by nothing.
376 ruff findings and 316 mypy errors, now **0 and 0**, with 246 of the mypy
errors *fixed* rather than silenced.

Beyond the Telegram bug, the real defects found:

- **8 × `F821`**: `Path` and `uvicorn.Server` used in quoted annotations while
  the import was function-local. No runtime `NameError`, but unresolvable to
  any checker and `get_type_hints()` would raise — annotations that were not
  even valid documentation.
- Seven row mappers annotated `tuple[Any, ...]` when aiosqlite hands them
  `sqlite3.Row`.
- `token_rotation.py` reached into `db._conn` from outside the class.
- `schedules.py` returned `session.agent_id` (`str | None`) from a `-> str`
  function.
- The connector helpers returned `(None, error)`, so a caller that skipped the
  `if err:` guard got an `AttributeError` deep inside a tool.

The single biggest mypy cause was `Database._conn`: 173 of 258 `union-attr`
errors were that one attribute, and `database.py` already contained the fix —
a `conn` property that asserts. Using it removed 168 at a stroke.

Six error codes remain disabled, **each with its reason in `pyproject.toml`**,
dominated by `SessionManager.db`, where the same trick is unavailable because
33 of its 59 read sites guard with `if self.db:` and an asserting property
would turn those guards into `AssertionError`s.

`ruff format` is deliberately **not** adopted: it would rewrite 124 of 177
files, which on hand-wrapped prose comments is an unreviewable diff that
destroys `git blame` for no behavioural gain.

### eslint, from 20 findings to 0 (F2)

Seven were a **config error** (`e2e/` linted as browser React when Playwright
is Node). Six were one deliberate house pattern — tested pure helpers exported
beside their component, plus shadcn's `buttonVariants` — so the rule is off,
with the reasoning recorded. Four were real. Three shared one cause: `headers`
rebuilt per render, so `useCallback` could not declare it without re-firing the
effect every render, which cost React Compiler the memoization entirely.

Two justified suppressions remain, each carrying its argument inline.

### One command for every gate (C1, F3)

`scripts/check.sh` runs seven gates and continues past failures, so one pass
shows every problem. `scripts/check-contracts.sh` is shared with lefthook and
generates into a temp dir, so a *check* never dirties the tree. **It earned its
place today**: the monitor routes drifted the generated contracts and the gate
caught it rather than anyone remembering.

The pre-commit hook now runs `-m "not real"`. It used to run the entire suite,
so **every commit depended on a signed-in CLI and the provider being up** —
invisible until C2 made the hermetic tier nameable.

### The real-CLI tier has a name (C2)

`pytest --collect-only` used to invoke the model: the 13 `*_real.py` suites
computed their skip condition at module scope, and the probe makes a real
`claude --print` call. **16.44 s → 0.80 s**, and `-m "not real"` replaces a
13-entry `--ignore` list.

The tier is richer than it looked: the login tests gate on *binary presence
only*, because a test of `codex login` must not require being logged in.

### Documentation (D, E)

`CLAUDE.md` **32,658 → 18,225 bytes (44%)**: three table rows were 15,832 of
them, enumerating in prose what every test covers. `architecture.md` was three
months stale and missing five subsystems — the whole Applications line.
`scripts/nag-architecture.sh` now warns (never blocks) when a commit touches
`server/` without it, which is the missing step that let it rot.

---

## Verification

| Gate | Result |
|---|---|
| ruff | clean |
| mypy | clean |
| pytest (hermetic) | **1,228 pass** (from 1,174) |
| eslint | clean |
| vitest | **202 pass** (from 195) |
| tsc | clean |
| generated contracts | in sync |
| Playwright (monitor) | **5 pass**, real browser |

Plus, outside the gates: B1 driven end-to-end against a real `claude` CLI
(tool names preserved, exit 0), A3 rehearsed against a copy of the production
database, and the Monitor page verified visually in a browser.

---

## What was deliberately not done

**A1 (SessionManager, 4,004 lines) and A4 (database.py, 2,646 lines).** Both
are real and both remain in the plan. Neither was started, because
`CLAUDE.md`'s first rule is that if the full thing is not worth doing right
now, do not start it — and a half-decomposed god class is worse than an intact
one. A4's groundwork is done and recorded: the migration inventory is mapped,
and `_has_column` already exists at 7 sites, so A3 finished a transition rather
than starting one.

**B2's frontend half.** The server can now window a transcript
(`max_seq`, `newest_first`), but `GET /sessions/{id}` still returns everything
and `ChatView` still receives it. Windowing the client needs scroll-anchoring
and duplicate-`seq` tests in a 1,579-line component; shipping the query without
them would have been the MVP the rules forbid.

**`union-attr` and five other mypy codes**, each with its reason recorded, most
of them concentrated in exactly the two files A1 and A4 will rewrite. Doing
them now means doing them twice.
