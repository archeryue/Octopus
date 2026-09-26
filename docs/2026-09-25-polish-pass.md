# What changed on 2026-09-25

A polish pass over Octopus: nothing here is a new product capability except
the monitor. The rest pays down debts that had accumulated under seven months
of fast feature work — and two of them turned out to be live defects.

Branch `polish-2026-09`, 16 commits, all gates green — and the whole plan,
§10 items 1 through 11, now done.
Plan and reasoning: [`plans/polish-2026-09.md`](plans/polish-2026-09.md).

---

## The five real bugs

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

**Every MCP tool call deadlocked for fifteen seconds and answered "failed to
reach Octopus".** B1's own defect, and it shipped with all seven gates green.
FastMCP dispatches a `def` tool straight on the event loop, and every tool body
here is sync and makes a *blocking* HTTP call back into this same process. As a
stdio subprocess that was correct — a blocking call from another process cannot
starve the server. Served in-process it is a deadlock: the loop cannot answer
the loopback request until the tool returns, and the tool cannot return until
the request is answered. Each call burned its own 15-second timeout, and any
MCP handshake a *second* session attempted in that window failed too, which is
how it presented — `CONNECT_TIMEOUT` on another agent's namespace discovery.

The fix is one adapter in `mcp_http.build_servers`: a sync tool body is wrapped
to run in a worker thread, with the calling context copied so the request's
verified scope still resolves. That is what FastAPI does for a `def` endpoint,
and it leaves nine tool modules and their unit tests untouched.

**Why no gate caught it.** The tool bodies are unit-tested by calling them
directly; the harness tests stop at the rendered config. Both pass while every
real call fails — there was nothing in between. There is now: a hermetic test
drives a tool over the mounted transport and asserts it answers, plus one that
fails if any namespace ever registers a body that would run on the loop.

**A held CLI process was reused after its MCP endpoint moved.**
`spawn_signature` decides whether a live process may serve the next turn, and it
exists precisely because everything a process bakes in at spawn — persona,
model, tool policy, credential, MCP set — cannot be changed afterwards. It held
the namespace *names*. Since B1 those names render into a URL carrying the
server's port and a bearer derived from the session and the access token, and
neither was in the signature. A process spawned against one endpoint was
therefore reused against another: it reports every namespace as unreachable, the
model loses its tools mid-conversation, and nothing in our own logs says why —
the only complaint is the CLI's, inside the model's reply.

Production rarely moves its port, so this surfaced in the real-CLI tier, where
each test serves its own ephemeral one: run alone, the schedule and delegation
tests passed; run after their neighbours, the CLI inherited a process pointed at
a dead socket and the model reported the tool as missing. The signature now
includes the callback environment those entries are built from, which covers the
port, the access token, and the session at once.

**Every cron with a day-of-week field fired a day late.**
`CronTrigger.from_crontab` looks like a crontab parser and is not, in the one
field where it matters: its `day_of_week` counts 0 = Monday, while crontab — and
every source a user or a model will quote — counts 0 = Sunday. So
`0 9 * * 1-5`, "weekdays at 9", fired **Tuesday to Saturday**, and
`0 9 * * 0`, "Sundays", fired on Monday. The tool's own docstring, the
natural-language prompt and the human-readable label all stated the crontab
convention correctly; only the trigger disagreed.

Found by the real-CLI test for the schedule tool: the model wrote
`0 9 * * 1-5` exactly as asked, and the tool answered "next Saturday". It even
ran `date` to check, and said Saturday was wrong, before reporting success.

All cron building now goes through one `schedule_ai.cron_trigger`, which
translates the day-of-week field to APScheduler's own day *names* — where there
is no ambiguity left to get wrong. No live schedule was affected: all five on
this box use `*` for day-of-week.

---

## Architecture — the two god classes (A1, A2, A4)

`Database` held 94 methods over ten subjects; `SessionManager` held 84 over at
least eight. Both are now one file per responsibility — `server/db/` (twelve
files, largest 661 lines) and `server/sessions/` (ten files) — composed as
mixins on one object, so **not a single call site changed**: `db.save_session`
is still `db.save_session`, and `server/database.py` / `server/session_manager.py`
remain as re-exports. A repository split would have read better on paper and
touched 200+ call sites to buy the same file-level separation.

**The dependency graph was the finding.** `sessions/turns.py` calls 26 methods
across the other six slices. Rather than switch off mypy's `attr-defined` — the
check that caught the database split dropping its class constants — those 26 are
declared as explicit contracts on the base class. The bodies never run (the
mixins precede the base in the MRO); they exist so "what does a slice need from
another slice" is a list you can read, which is exactly what a 4,000-line class
hides.

**`_run_backend`, 473 lines, was the gravitational centre** — and the reason it
could not be split is that eight of its branches shared eleven local variables.
Naming that state (`_Attempt` for one CLI invocation, `_Turn` for what survives
across invocations of one logical turn) is what made both halves extractable:
the event loop became `for frame in await self._handle_stream_event(...)`, and
the failure-classification tree became five methods that *return* a decision
(`stop`, or `again(prompt, delay)`) instead of yielding from inside an async
generator. 473 lines → 68, with the ordering that matters — a watchdog timeout
read before anything retryable, auth before the retries, a dangling resume id
before "transient" — stated in one docstring instead of inferred from
indentation.

One latent bug fell out: `steer_writer` was bound *inside* the `try`, so a
spawn that raised unwound through an unbound name in the `finally` and buried
the real error.

**A2**: routers now take the session manager as a FastAPI dependency
(`server/deps.py`) rather than importing the process singleton. The parameter is
deliberately named `session_manager`, so no line inside a route changed; what
changed is that a test overrides one dependency instead of monkeypatching a
module global in each router, and nothing in the request path is bound to a
particular instance. The singleton stays for the background paths — the
scheduler, the delegation manager — which genuinely want the one live object.

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

### The database stops doing unbounded work (B3–B5)

- `load_messages` gained `max_seq` and `newest_first` — the range bounds the
  window below is built on. Fork replay used to load a whole transcript and drop
  the tail in Python: on the largest session here, 4,600 rows read to keep ten.
- `append_message` deferred its commit with **nothing bounding it**: three
  explicit flush sites, no timer, and the production WAL sitting at 7 MB as a
  result. Now ~0.5 s or 32 rows, whichever trips first.
- `busy_timeout` and an explicit `synchronous=NORMAL`, so "database is locked"
  waits instead of raising, and the durability story is stated rather than
  inherited.

### A transcript arrives in a window (B2, both halves)

`GET /api/sessions/{id}` returned every message a session had, every time it was
opened: 46,448 rows live here, the largest session holds 4,873, and the client
was handed all of them to render. It now returns the most recent 200 plus a
cursor (`oldest_loaded_seq`, `has_more_messages`), and `GET …/messages?before_seq=`
serves the rest. Archived sessions read the same way — and their `message_count`
and dedup baseline now come from a `COUNT(*)` rather than `len(messages)`, which
stopped being the count the moment a window was all that was fetched.

The risk in B2 was never the query; it was the client. Two things go wrong when
a virtualized list grows at the top, and both are invisible to a unit test: the
view jumps, and a message renders twice at the seam. The view is held by
Virtuoso's `firstItemIndex`, decremented by exactly the number of messages
prepended in the same tick the longer list lands in; the seam is held by seq —
a page that overlaps what is already held is dropped rather than merged, and a
page that adds nothing sets `has_more` to false rather than polling forever.

Both are asserted in a real browser against a 260-message session: it opens at
message 259, reaching the top loads the 60 behind the window, the message that
was on screen is still on screen afterwards, and it is there exactly once.
`applyTranscript` is one function because three places load a snapshot —
selecting a session, the WebSocket reconnect refetch, the archived viewer — and
each owes the store the messages, the dedup baseline *and* the window; a fourth
caller cannot now get two of the three right.

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

### The silent handlers are gone (F4)

26 `except Exception: pass`. The migration cluster went with A3; the remaining
twenty were two shapes, repeated. "Cancel a task, then await it so it unwinds"
appeared eight times as `except (asyncio.CancelledError, Exception): pass` —
right about the cancellation, silent about everything else, on teardown paths
which is precisely where a last exception used to disappear. Both shapes are now
one helper each in `server/aio.py`: the cancellation is swallowed because it is
expected, anything else is logged, and neither can raise, because every caller
is releasing a process or a lock.

The rest were narrowed to what they actually meant: `JobLookupError` for
removing a schedule that has no job, `WebSocketDisconnect`/`RuntimeError` for
broadcasting to a tab that closed, `IntegrityError` for the one-time agent
rename that a name collision should skip. `server/` now contains **zero** broad
silent handlers, and the one place catching broadly on purpose — the metrics
sink, which must never break a turn — says so in a comment.

A side effect worth the note: the `PytestUnraisableExceptionWarning` that the
suite had been carrying (a subprocess transport collected after its loop closed)
went with it. The suite is now warning-free, which is the state in which a *new*
warning is worth reading.

### The plans index generates itself (D2)

`docs/plans/` had 23 documents and no way to tell a shipped design from an
in-progress one from the outside, while the hand-written list in
`docs/README.md` had drifted to 13 of them — the same failure mode as the
hand-maintained test inventory E1 deleted. Each plan now states its status on
one machine-readable line under its title, `scripts/gen-docs-index.py` renders
the table, and `check.sh` plus a lefthook glob fail if it has drifted. A new
plan cannot be added without appearing in the index.

### Two smaller ones

**C3**: the httpx per-request `cookies=` deprecation is gone — the cookie is set
on the client, which is also a truer model of what a browser does.

**The Telegram bridge was removed** (not a plan item): Octopus works from a
phone browser now, which is what the bridge existed to provide. Whole — code,
tests, settings, and the `bridge_mappings` table, because a table nothing reads
is a question every future reader has to answer. **Breaking for an existing
install**: `Settings` is `extra="forbid"`, so an `.env` still carrying
`OCTOPUS_TELEGRAM_*` fails to start.

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

### One open item in the real-CLI tier

Five of the 34 real-model tests **pass individually and fail when another
`claude` test has already run in the same pytest process**: the two delegation
cases that need a child CLI to call a tool, and the three schedule-tool cases.
The CLI reports every namespace as `failed` (which Octopus now logs — it used to
be visible only inside the model's reply).

What has been *ruled out*, each by measurement rather than reasoning:

| Suspect | Finding |
|---|---|
| the server | three sequential servers, five namespaces each: 0.01 s handshakes, real tool calls answered |
| the port in argv | matches the test's server, verified from the spawn log |
| the CLI itself | three sequential `claude --print` runs against one live mount: all five connected every time |
| host MCP config leaking in | real, and fixed (`--strict-mcp-config`); the failure survives it |
| a moving port between tests | fixed port tried; no difference |
| memory pressure | 11.8 GB free, 16 cores, at the moment of failure |
| leftover test CLIs | swept after every real test now; none survive |

So the product path is verified three independent ways — the hermetic transport
test, a real `claude`, and a real `codex` both calling `mcp__schedule__*` — and
what is left is state inside the CLI's own process that we cannot read from here.
The next step is its `--debug` MCP log, captured from inside the turn engine
rather than from a standalone invocation, which is where the difference must be.

Recorded rather than papered over: these tests assert something true, they are
not marked skip, and the tier is run by hand, not by a gate.

---

## What was deliberately not done

**`union-attr` and five other mypy codes**, each with its reason recorded in
`pyproject.toml`. The largest remaining cause is `SessionManager.db`, where the
asserting-property trick that removed 168 errors from `Database._conn` is
unavailable: 33 of its 59 read sites guard with `if self.db:`, and an asserting
property would turn those guards into `AssertionError`s.

**Rewriting the tool bodies onto an async HTTP client.** Threading the sync
bodies fixed B1's deadlock and keeps nine modules and their unit tests as they
are. The ceiling it accepts is anyio's default 40-thread limiter, i.e. 40
concurrent *blocking* tool calls — `mcp__ask__user` can hold one for as long as
a human takes to answer. That is far above anything one box runs, and the
rewrite buys nothing until it is in sight.

**Everything §11 of the plan defers**, unchanged: alerting thresholds (they need
30 days of collected data first), a message-retention policy for `octopus.db`
(product data, so a user decision rather than cleanup), e2e in an automated gate,
and splitting `ChatView.tsx`.
