# Tech Plan: First-Class Harness Layer

Status: implemented (2026-05-23) — single `Harness` + `RuntimeProfile` model,
**all** callers migrated (turns, one-shot/`/schedule`, login, export/import),
`server/backends/` deleted. Grep-verified: zero backend-kind branching and
zero feature-code `claude`/`codex` spawns outside `server/harness/`. The
shared auth modules (`oauth_login`/`oauth_providers`, also used by connectors)
stay top-level, *wrapped* by the harness login drivers — not relocated.
Verified green at implementation time; current suite counts: ~764 backend
tests + 64 frontend (vitest) + tsc + 62 e2e (Playwright).

## 0. Why this exists

Octopus has a good *per-turn* backend abstraction (`BackendBase` in
`server/backends/`), but model/runtime interaction as a whole is **not**
a single architectural boundary. Concretely, today:

- **Schedule parsing shells out to Claude directly.**
  `schedule_ai.run_claude_oneshot` (`schedule_ai.py:260`) hardcodes
  `claude --print` and Claude-only auth env vars. So natural-language
  `/schedule` runs on Claude *regardless of the agent's backend*, and a
  Codex agent literally cannot use it — `routers/agents.py:204` papers
  over this by nulling the credential when `credential.backend !=
  "claude-code"`.
- **Capability differences are smuggled in as ad-hoc flags.**
  `wants_premature_exit_recovery` (a Claude-CLI-bug workaround as a
  bool, `base.py:61`), `answer_question` returning `False` by default
  ("Codex can't do AskUserQuestion", `base.py:90`), tool allow/deny
  passed only to Claude (`session_manager.py:1174`), `credential_home`
  passed only to Codex (`session_manager.py:1185`).
- **Login is two unrelated mechanisms with no shared contract.** Claude
  OAuth (`oauth_login.py` + `oauth_providers.py`, HTTP/redirect) and
  Codex device-auth (`codex_login.py`, subprocess + stdout scraping).
  `routers/credentials.py` branches on kind to pick the right one.
- **Session export/import is Claude-only and unguarded.** `cli.py`
  handoff/pull + `jsonl_parser`/`jsonl_writer` assume the Claude Code
  JSONL schema; nothing declares that Codex has no equivalent.
- **The two turn adapters duplicate context assembly.** Each of
  `claude_code.py` and `codex.py` independently composes the system
  prompt (persona + Octopus-tools blurb + connector blurb), selects the
  in-app MCP set from `{viewer,bg,ask}`, and builds `callback_env`. Only
  the *rendering* (JSON `--mcp-config` vs `-c mcp_servers.*` TOML) is
  genuinely backend-specific.

The right boundary is a **harness layer**: all model/runtime interaction
goes through it, feature code never shells out to `claude`/`codex` and
never decides which binary to invoke, and the layer adapts Claude Code,
Codex, and future harnesses behind one contract with **explicit
capabilities**. This plan does the *full* migration — turn execution,
one-shot/schedule parsing, login, and export/import — not a partial one.
A half-migrated boundary is the leaky-abstraction risk (§15) we are
explicitly avoiding.

## 1. Goals

1. **One way in.** Every model/runtime operation — streaming turns,
   one-shot model calls, schedule parsing, login, transcript
   export/import, availability checks — goes through the harness layer.
   No `claude`/`codex`/provider-SDK invocation and no
   `if backend == "..."` dispatch outside the harness adapters.
2. **Capability over assumption.** Each harness *declares* what it
   supports. Feature code asks `harness.supports(cap)` and gets an
   explicit "unsupported" instead of a silent fallback to another model
   or the host login.
3. **Feature owns intent, harness owns execution.** Feature code says
   *what* it wants (a turn for this spec, parse this text, log in, export
   this session); the harness owns CLI/SDK invocation, auth
   materialization, prompt formatting, MCP wiring, and stream
   translation.
4. **De-duplicate context assembly.** Composing the turn (system prompt,
   MCP server selection, callback env) happens once in shared code; each
   adapter only *renders* the neutral spec to its argv. This is also what
   makes the future memory feature a single-point addition.
5. **No behavior regressions, full test parity** (CLAUDE.md "After Every
   Code Change": 639 backend + 34 frontend + tsc + 59 e2e, all green).

## 2. Non-goals

- Not redesigning product logic (sessions, agents, schedules semantics)
  beyond what the boundary requires.
- No remote service / separate process for the harness layer — it's an
  in-process module like `backends/` is today.
- No "automatic model router" picking providers on its own outside the
  harness selection that already exists (agent's chosen kind).
- Not requiring every harness to support every operation — derived
  predicates (§4) make the few real gaps explicit instead of silent.

## 3. The model: ONE `Harness`, configured by a `RuntimeProfile`

There are **no** `ClaudeCodeHarness` / `CodexHarness` subclasses. That was
a bad smell — two parallel classes overstate a difference that is mostly
serialization detail, and they invite a hand-maintained matrix of "flags
that differ per class." **VM0 (the reference codebase) confirms the
better shape**: it has no per-framework classes at all — a framework is a
string literal (`SUPPORTED_FRAMEWORKS = ["claude-code","codex"]`) and all
variation lives in **data records keyed by that string**
(`FRAMEWORK_DEFAULTS`, `BACKEND_COMMANDS`, `FRAMEWORK_INSTRUCTIONS_FILENAMES`)
plus a little co-located dispatch in the one command-builder. We adopt the
same data-driven shape (§11a).

Three roles, **one class each** — the per-framework things are *values*,
not *subclasses*:

- **`Harness`** (one concrete class, kind-level, stateless — one instance
  per backend in a registry). The single front door for everything
  model-related. It holds a `RuntimeProfile` and exposes:
  - `backend: str` — the persisted identifier (`"claude-code"`/`"codex"`,
    kept per D1).
  - `is_available() -> bool` — binary resolvable on this host (replaces
    `main.py`'s ad-hoc `_which_with_fallback("codex")`).
  - `create_run(config) -> HarnessRun` — build the per-turn run engine.
  - `run_oneshot(prompt, ctx) -> str` — lean non-interactive call (§6).
  - `parse_schedule(text, ctx) -> ParsedSchedule` (§6).
  - `login -> LoginDriver` (§7); `export_session`/`import_session` (§8).
  - **Derived predicates** instead of a declared capability matrix:
    `can_export` ⇐ `profile.transcript_codec is not None`, etc. (§4).

- **`RuntimeProfile`** (one frozen dataclass; **two values**:
  `CLAUDE_CODE` in `claude_code.py`, `CODEX` in `codex.py`). Bundles the
  per-framework data + the few irreducible behavioral pieces as
  references — VM0's `Record<framework, {...}>` in Python:
  ```python
  @dataclass(frozen=True)
  class RuntimeProfile:
      backend: str                 # "claude-code" | "codex"
      binary: str                  # "claude" | "codex"
      tools_prompt: str            # in-app-tools blurb (per-framework wording)
      credential_style: str        # "env_secret" | "home_dir"
      premature_exit_recovery: bool  # internal Claude-CLI bug workaround (not a product feature)
      close_stdin_after_start: bool  # codex=True (EOF so it uses the argv prompt), claude=False
      build_turn_argv: Callable[[TurnContext], tuple[list[str], dict]]
      new_event_parser: Callable[[], EventParser]   # fresh per-turn parser collaborator
      build_oneshot_argv: Callable[[OneShotContext], tuple[list[str], dict]]
      parse_oneshot_stdout: Callable[[str], str]
      login: LoginDriver
      transcript_codec: TranscriptCodec | None      # None for codex (D-decision)
  ```

- **`HarnessRun`** (one concrete class — the renamed shared
  `SubprocessJsonlBackend` engine; **not** subclassed). Owns the
  subprocess + queue + reader loop exactly as today. The two things that
  used to be `@abstractmethod` overrides become profile-driven:
  - argv: `start()` calls `self._profile.build_turn_argv(ctx)`,
  - parsing: `on_stdout_line` delegates to `self._parser.parse(obj)`
    (the parser is `profile.new_event_parser()`, holding the tiny
    per-turn state — the captured session/thread id),
  - stdin: gated on `profile.close_stdin_after_start`.
  `wants_premature_exit_recovery` is deleted; the run loop reads
  `harness.profile.premature_exit_recovery`.

The only per-framework *classes* that remain are the genuinely
irreducible collaborators — **`EventParser`** (Claude stream-json vs Codex
`exec --json` are different protocols) and **`LoginDriver`** (oauth-redirect
vs device-code). These are narrow codec/driver objects *referenced by* a
profile, not harness concepts. (This is the honest caveat already agreed:
the two parsers/logins don't vanish — they move from subclasses to
composed collaborators.)

## 4. Capabilities are *derived*, not declared

No `HarnessCapability` enum, no per-class flag matrix (that's the smell).
After the corrections below, the two harnesses are **near-symmetric**, so
a declared matrix would mostly be ✓/✓. Feature code asks a small set of
**derived predicates**, computed from what the profile provides:

| Question | Derived from | claude-code | codex |
|---|---|---|---|
| run turns / resume / MCP / model override | always (both have an engine + profile) | ✓ | ✓ |
| `oneshot` / `parse_schedule` | `build_oneshot_argv` present | ✓ | ✓ *(D2)* |
| login | `profile.login` (+ its `method`: oauth_redirect / device_code) | ✓ | ✓ |
| `can_export` / `can_import` | `profile.transcript_codec is not None` | ✓ | — |
| premature-exit recovery | `profile.premature_exit_recovery` *(internal, not a product capability)* | on | off |
| tool allow/deny | the Claude argv builder emits the flags; Codex's ignores them (graceful degrade, exactly as today) | enforced | ignored |

Two corrections that collapse most of the apparent asymmetry:
- **`question_answer` is NOT harness-specific.** AskUserQuestion is *our*
  `ask` MCP server (`mcp__ask__user`), already wired into the
  `{viewer,bg,ask}` set both harnesses get — Codex can already ask. The
  `backend.answer_question` method + `question_request` *backend event*
  are vestigial leftovers of a disabled native path; drop them.
- **`premature_exit_recovery` is not a capability.** It's a workaround for
  a specific Claude-CLI bug — internal to the Claude run's stream
  handling, expressed as one profile flag the run loop reads, not a
  product feature anything "supports."

The only genuine "unsupported" surface left is transcript export/import on
Codex, which fails explicitly via the `can_export`/`can_import` predicate.

## 5. `TurnSpec` + shared context assembly (the de-dup)

`session_manager` builds **one neutral `TurnSpec`** per turn; the harness
composes the shared parts and the run only renders. `TurnSpec` carries
the resolved inputs already gathered in `_run_backend`
(`session_manager.py:989–1009`):

```python
@dataclass
class TurnSpec:
    session_id: str
    working_dir: str
    prompt: str                 # may be "continue" on recovery
    resume_id: str | None
    system_prompt: str | None   # agent persona
    model: str | None
    mcp_servers: list[str] | None       # selected built-in subset (None = all)
    tool_allow: list[str] | None
    tool_deny: list[str] | None
    connectors: list[tuple[ConnectorBase, ConnectorInstallation]]
    credential: HarnessCredential | None # resolved (§9)
    callback_env: dict[str, str]         # api_base, auth_token, session_id, PYTHONPATH
```

**Shared in `assembly.py`** (moved out of both `build_args`), producing
the neutral pieces passed into `build_turn_argv` via `TurnContext`:
- selecting in-app MCP servers from `{bg,ask,ask_agent}` per `mcp_servers`
  (the original set was `{viewer,bg,ask}`; `viewer` was removed when
  `/showme` became a client-driven REST resolver, and `ask_agent`
  landed with [`agent-collaboration.md`](agent-collaboration.md)),
- merging connector MCP entries (`mcp_entry`, already neutral),
- composing the system prompt = `persona + profile.tools_prompt
  + render_connectors_blurb(connectors)`. The Octopus-tools blurb stays
  per-framework *text* (`profile.tools_prompt`: `_OCTOPUS_SYSTEM_PROMPT`
  vs `_OCTOPUS_SYSTEM_PROMPT_CODEX`) but the *composition* is shared, so
  only the blurb wording differs.

**Rendered per profile** (`build_turn_argv`, the only backend-specific part):
- MCP entries → `--mcp-config` JSON (Claude) vs `-c mcp_servers.*` TOML
  (Codex),
- system prompt → `--append-system-prompt` (Claude) vs `-c
  developer_instructions` (Codex),
- tool allow/deny (Claude only), model flag, resume flag, binary +
  subcommand, credential application (§9).

This is the seam memory plugs into later in exactly one place (base
assembly: add the memory MCP entry + inject the index).

## 6. One-shot calls + schedule parsing

`Harness.run_oneshot(prompt, ctx)` is the single primitive for
non-interactive, tool-free model calls: it builds argv via
`profile.build_oneshot_argv`, runs the subprocess, and extracts text via
`profile.parse_oneshot_stdout`. Claude's profile renders today's `claude
--print --output-format=json` (`schedule_ai.py:260–313` becomes the
Claude profile's oneshot fns); Codex's renders `codex exec --json` and
extracts the final `agent_message` text from the event stream (D2). It is
the home for any future model-backed utility (memory summarization, etc.)
so nothing ever shells out directly again.

`schedule_ai` keeps its **pure** helpers (rigid parse, JSON extraction,
validation, labels — all backend-agnostic and unit-tested). The AI path
changes: `parse_schedule_text` takes a `Harness` and calls
`harness.run_oneshot(...)` instead of the module-level
`run_claude_oneshot`. The `runner` injection seam is preserved for tests
(a fake harness). `routers/agents.py` resolves the agent's harness and
passes it in — both backends now parse (D2), so no kind-nulling and no
silent Claude fallback.

## 7. Login — one protocol, two methods

Each profile carries a `login: LoginDriver` whose `method` declares its
shape. Both existing managers (`OAuthLoginManager`, `CodexLoginManager`)
are wrapped to conform to one protocol with a superset surface, so
`routers/credentials.py` stops branching on kind:

```python
class LoginMethod(StrEnum):
    oauth_redirect   # claude: authorize URL → user pastes code
    device_code      # codex: verification URL + user code → poll

class LoginDriver(Protocol):
    method: LoginMethod
    async def begin(self, label: str | None) -> LoginStart   # {login_id, authorize_url?, verification_url?, user_code?, requires_code: bool}
    async def submit_code(self, login_id: str, code: str) -> LoginState  # only when requires_code
    async def poll(self, login_id: str) -> LoginState
    async def cancel(self, login_id: str) -> None
    async def persist(self, login_id: str, db) -> str        # write the backend_credentials row, return id
```

The router becomes harness-driven: `harness = get_harness(backend)`, then
calls `harness.login`'s generic methods. The frontend renders the right
UI from `login.method` (an explicit declared attribute, not a hardcoded
`if codex`). The genuinely different internals (HTTP redirect vs
subprocess+scrape) stay inside each profile's `LoginDriver`.
Per-credential `CODEX_HOME` resolution (`codex_home_for`) moves under the
Codex profile.

## 8. Session export / import

Export/import is a profile feature: `profile.transcript_codec` (Claude
owns the JSONL format — `jsonl_parser`/`jsonl_writer` become its codec;
Codex's is `None`). `Harness.can_export`/`can_import` derive from it.
`cli.py` handoff/pull and the import route ask the harness; an
unsupported harness (Codex) returns an explicit error rather than
silently producing a malformed Claude transcript. (`cli.py` runs
client-side against the REST API; it selects the harness for the target
session's kind via the API, keeping "one way in" intact.)

## 9. Credential resolution

Credential *resolution* (DB lookup + decrypt + OAuth refresh) stays
centralized in a `CredentialResolver` (extracted from
`session_manager.resolve_credential_by_id`, `session_manager.py:1253`).
It produces a neutral `HarnessCredential` whose shape is driven by the
harness's `credential_style`:
- `env_secret` (Claude): `{auth_type, secret}` → applied as
  `ANTHROPIC_API_KEY` / `CLAUDE_CODE_OAUTH_TOKEN` by the run.
- `home_dir` (Codex): `{home_dir}` → applied as `CODEX_HOME`. The
  `_codex_home_for` logic (`session_manager.py:1200`) moves here.

`session_manager` no longer branches on kind for credentials; it asks the
resolver, which asks the harness. (`BackendCredential` → `HarnessCredential`,
gaining the optional `home_dir`.)

## 10. Registry + availability

```python
# server/harness/registry.py
_REGISTRY: dict[str, Harness] = {}      # backend kind → Harness(profile)
def register(h: Harness) -> None: ...
def get_harness(backend: str) -> Harness     # raises on unknown kind
def available_backends() -> list[str]        # is_available() filtered — feeds the frontend picker
```

`claude_code.py` and `codex.py` each register `Harness(CLAUDE_CODE)` /
`Harness(CODEX)` on import (mirrors the connector registry). `main.py`'s
availability probe and the frontend's `availableBackends` are computed
from `available_backends()`.

## 11. Naming & file layout

Rename the **code abstraction** to "harness" (the docs and team language
already use it; mixing `backend` in code with `harness` in prose is a
split-vocabulary smell). The persisted/wire field stays `backend` (D1).

```
server/harness/
  __init__.py        # public exports + registry
  base.py            # Harness (1 class), HarnessRun (1 concrete engine), HarnessEvent, HarnessCredential
  profile.py         # RuntimeProfile dataclass; TurnContext/OneShotContext; EventParser ABC; TranscriptCodec proto
  login.py           # LoginDriver protocol, LoginMethod, LoginStart/LoginState DTOs
  assembly.py        # shared context-assembly: callback_env, MCP-server selection+merge, system-prompt composition
  registry.py        # register / get_harness / available_backends
  claude_code.py     # CLAUDE_CODE profile: argv + ClaudeEventParser + oneshot fns + JSONL codec + oauth LoginDriver; registers Harness
  codex.py           # CODEX profile: argv + CodexEventParser + oneshot fns + codec=None + device-code LoginDriver; registers Harness
```

There are **no** `ClaudeCodeHarness`/`CodexHarness`/`ClaudeCodeRun`/
`CodexRun` classes — the per-framework things are the two `RuntimeProfile`
*values* plus their `EventParser`/`LoginDriver` collaborators. Renames of
the existing types: `BackendBase` (the ABC) is **removed** — its
interface is now the single concrete `HarnessRun` (the merged
`SubprocessJsonlBackend` engine); `BackendEvent`→`HarnessEvent`,
`BackendCredential`→`HarnessCredential` (gains optional `home_dir`).
`BackendKind` enum + `backend` field/values are **unchanged** (D1).
`oauth_login.py`/`oauth_providers.py`/`codex_login.py`/`jsonl_*` stay as
modules, wrapped/imported by the two profiles (their LoginDriver / codec).

### D1 — persisted/wire field name — DECIDED: keep `backend`

The DB columns `agents.backend` / `sessions.backend` /
`backend_credentials.backend` and the API field `backend` (values
`"claude-code"`/`"codex"`) store the **harness kind**. **Decision: keep
the field name `backend`** and the `BackendKind` enum / value strings
unchanged — they are the stable external/persisted identifier of the
harness kind. We rename only the *code* abstraction (classes, module
dir). No DB migration, no `contracts.ts`/API churn. Everywhere a kind is
read, it feeds `get_harness(backend)`. (`BackendKind` enum keeps its name
since it's the wire contract serialized to `contracts.ts`.)

### D2 — Codex `oneshot`/`schedule_parse` — DECIDED: add now

Codex gets `oneshot` (via non-interactive `codex exec --json`, text
extracted from its event stream) and therefore `schedule_parse`. So
natural-language `/schedule` works on Codex agents in this refactor.
`run_oneshot` is a **lean** invocation (no MCP, no Octopus-tools blurb,
no connectors) — distinct from a full turn run. The Codex credential
(home dir) is applied for the oneshot exactly as for a turn. New
Codex-oneshot tests (fake + real-CLI-gated) are added alongside the
migration.

## 12. Caller migration (every off-abstraction site)

- **`session_manager._make_backend`** → `_make_run`: build a `RunConfig`
  from the agent, `get_harness(session.backend).create_run(config)`.
  Remove the kind `if/else` and the Claude-vs-Codex constructor
  divergence.
- **Run loop** (`session_manager.py:998–1124`): replace
  `backend.wants_premature_exit_recovery` with
  `harness.profile.premature_exit_recovery`. Drop the vestigial
  `answer_question`/`question_request` *backend* path (the `ask` MCP flow
  is untouched).
- **`_resolve_credential`/`resolve_credential_by_id`** → `CredentialResolver`
  producing a `HarnessCredential` (secret or home_dir per
  `profile.credential_style`); `_codex_home_for` moves into the Codex
  profile's resolution.
- **`routers/agents.py`** schedule-from-text: `get_harness(agent.backend)`
  then `harness.run_oneshot` via `parse_schedule_text` — both kinds parse
  (D2); delete the `credential.backend != "claude-code"` nulling.
- **`routers/credentials.py`**: harness-driven login (`harness.login`,
  §7) + harness-driven delete cleanup.
- **`cli.py`** handoff/pull + import route: gated on
  `harness.can_export`/`can_import`.
- **`main.py`**: availability via `available_backends()`.
- **`models.py`/`contracts.ts`/frontend**: unchanged (`backend` field
  kept, D1).

## 13. Phased implementation (each phase leaves the suite green)

- **Phase 1 — package + engine.** New `server/harness/`: `HarnessEvent`,
  `HarnessCredential`, `RuntimeProfile`/`TurnContext`/`OneShotContext`,
  `EventParser` ABC, the merged `HarnessRun` engine (from
  `subprocess_jsonl.py`), `Harness`, `assembly.py`, `registry.py`,
  `LoginDriver` protocol. Unit tests for the engine + assembly + registry.
- **Phase 2 — Claude profile.** `CLAUDE_CODE` profile: `build_turn_argv`
  + `ClaudeEventParser` (ported verbatim from `claude_code.py`), oneshot
  fns (from `schedule_ai.run_claude_oneshot`), JSONL `TranscriptCodec`
  (from `jsonl_parser`/`jsonl_writer`), oauth `LoginDriver` (wrapping
  `OAuthLoginManager`). Behavior byte-identical; existing Claude tests
  retargeted and green.
- **Phase 3 — Codex profile.** `CODEX` profile: `build_turn_argv` +
  `CodexEventParser` (ported from `codex.py`), oneshot fns (`codex exec
  --json`, extract final `agent_message` — D2), `transcript_codec=None`,
  device-code `LoginDriver` (wrapping `CodexLoginManager`) +
  `CODEX_HOME` resolution.
- **Phase 4 — migrate callers** (§12): `session_manager`, routers,
  `cli.py`, `main.py`, `schedule_ai`, credential resolver. Remove all
  `if backend == "..."` branching and ad-hoc flags. Delete
  `server/backends/`.
- **Phase 5 — tests + docs.** Retarget every test in the Part-V
  inventory; add the new tests (§14). Update CLAUDE.md (structure,
  test-count table, conventions). No `contracts.ts`/migration churn
  (D1).

## 14. Tests (mirror the existing layout; zero failures)

- **New:** registry + derived predicates (`can_export` etc.); the merged
  `HarnessRun` engine driven by a fake profile; `assembly.py` (system
  prompt composition + MCP selection/merge); `run_oneshot` for **both**
  profiles (fake + real-CLI-gated, incl. Codex per D2); `LoginDriver`
  conformance for both methods; export/import gating (explicit error on
  Codex).
- **Retargeted, must stay green:** every test in the Part-V inventory —
  `test_backend_claude_code{,_real}.py`, `test_backend_codex{,_real}.py`,
  `test_backends_subprocess.py`, `test_backend_connectors.py`,
  `test_session_manager.py`, `test_schedule_ai{,_real}.py`,
  `test_oauth_login.py`, `test_codex_login{,_real}.py`,
  `test_credentials{,_api}.py`, `test_agents_api.py`, `test_api.py`,
  `test_cli.py`, `test_jsonl_*`, `test_bg_tasks.py`, `test_file_viewer.py`,
  `test_large_prompts.py`, `test_migration_backfill.py`. The
  scripted-fake-CLI tests change from *subclassing* `ClaudeCodeBackend`/
  `CodexBackend` to constructing `HarnessRun` with the real profile and a
  fake binary.
- **Frontend/e2e:** unchanged (no contract change, D1) — `sessionStore`,
  `web/e2e/codex.spec.ts`, `new-features.spec.ts` must stay green.
- All four suites (pytest / vitest / tsc / Playwright) green before any
  commit.

## 15. Risks

- **Partial migration** is the headline risk — some code through the
  harness, some still direct. Mitigation: §12 enumerates every site;
  Phase 4's acceptance is "grep finds zero `claude`/`codex` subprocess
  spawns and zero `backend ==`/`backend in (...)` branching outside
  `server/harness/`."
- **Behavioral drift in the ported parsers.** The two `EventParser`s and
  `build_turn_argv`s must be byte-faithful ports. Mitigation: the
  existing backend tests are the oracle — they pass unchanged in intent.
- **Login over-fitting.** The two flows differ; the superset protocol
  must not force false symmetry. Mitigation: `requires_code` +
  `login.method` model the real difference explicitly.

## 16. Acceptance criteria

- All model/runtime interaction goes through `server/harness/`.
- No feature code shells out to `claude`/`codex` or branches on backend
  kind (grep-verifiable).
- **One** `Harness` class + two `RuntimeProfile` values (no per-framework
  Harness/Run subclasses); capabilities derived, not declared.
- `/schedule` (both kinds), login, and export/import are harness-driven;
  Codex export/import fails explicitly via `can_export`/`can_import`.
- Tests cover supported and unsupported paths; all suites green.
