# Codex Backend — Tech Plan

## 0. Why this exists, and how grounded this is now

This is `future-features.md` item #1 — the second backend. The whole
`BackendBase` / `SubprocessJsonlBackend` abstraction (`server/backends/`)
was built for exactly this: `ClaudeCodeBackend` is one concrete
subclass, Codex is the second, and the rest of the system can't tell
them apart.

**The mental model: a Codex session is a Claude session that happens to
spawn a different binary.** Same chat UX, same in-app tools (`/showme`,
`bg_run`, `ask_user`), same schedules, bridges, archive, attachments,
and message persistence. The only things that change are (a) which CLI
we spawn, (b) how we translate its JSONL into `BackendEvent`, (c) how we
inject our MCP tools + instructions, and (d) how we authenticate.
Everything downstream stays on the normalized `BackendEvent` vocabulary.

**Grounding status (good news).** Two facts changed since the first
draft:

1. **`codex` 0.132.0 is installed here** (`~/.nvm/versions/node/v22.16.0/bin/codex`).
   The `exec` flag surface below is read off the real `--help`.
2. **VM0 already ships Codex support and we read its source.** VM0's
   `codex-event-parser.ts` gives us the real `codex exec --json` event
   schema, and its `guest-agent` `command.rs` gives a battle-tested
   spawn command. Those close what used to be guesswork.

What's left genuinely open is **the auth/login flow** (a product
decision, §7) and a final **confirm-against-a-live-run** pass once a
subscription is logged in (§12). We do not write `codex.py` against
anything still marked unverified.

**VM0 reference files** (read-only source we borrowed from; paths under
`/home/start-up/vm0`):
- `turbo/apps/cli/src/lib/events/codex-event-parser.ts` — event schema.
- `crates/guest-agent/src/cli/command.rs` — `build_codex_args`.
- `crates/guest-agent/src/cli/mod.rs`, `codex_auth.rs` — `CODEX_HOME`
  + the (microVM-specific) auth trick we deliberately do **not** copy.

---

## 1. Goals

- A user with `codex` installed, logged into their **own ChatGPT
  subscription**, can create a Codex session and chat with it exactly
  like a Claude session.
- The three Octopus MCP tools (`viewer`, `bg`, `ask`) work inside Codex
  sessions with no feature loss. (VM0 does *not* do this; it's our work.)
- Claude sessions are **byte-for-byte unchanged** — every existing test
  stays green and the Claude path grows no new branches.
- The Claude-CLI-specific seams (premature-exit recovery, the
  `claude_session_id` name) become backend-agnostic, not `if backend ==`
  forks.

## 2. Non-goals (v1)

- **API-key (`OPENAI_API_KEY`) auth.** Explicitly out — the user wants
  their ChatGPT subscription used, not metered API billing. (VM0 keeps a
  `codex login --with-api-key` fallback; we don't need it.)
- **A per-tool approval UI for Codex.** We run Codex headless with
  approvals/sandbox bypassed (§5.6), so no approval round-trip ever
  reaches the frontend and `ToolApproval` simply never fires for Codex —
  zero UI change.
- **Codex's `features.memories` / cloud features.** VM0 enables
  `-c features.memories=true`; Octopus has its own memory story and
  won't opt in for v1.
- **Multi-account VM0-style token injection.** The placeholder-JWT +
  egress-firewall mechanism (`vm0/.../codex_auth.rs`) is a multi-tenant
  microVM trick; single-user local Octopus uses a real on-disk login.

## 3. The real starting state (correcting the future-features sketch)

The `future-features.md` #1 blurb says "_make_backend dispatches on
`session.backend`." Half-true. Precisely:

**Already in place:** `BackendKind` enum `claude-code | codex`
(`server/models.py:152`); `backend_credentials.backend` column + index
(`server/database.py:61`); `_resolve_credential` already returns
`BackendCredential(backend=row["backend"], …)`
(`server/session_manager.py:1031,1040`); the frontend already renders
Claude/Codex credential badges.

**The actual gap:**
- **Sessions carry no backend.** Not in `CreateSessionRequest` /
  `SessionInfo` (`models.py:15,29`), not on the `Session` dataclass
  (`session_manager.py:83`), not in the `sessions` table. Backend is
  currently *implicit from the credential*.
- `_make_backend` hardcodes `ClaudeCodeBackend` (`session_manager.py:968`).
- The resume handle is named `claude_session_id` everywhere (DB column,
  `Session` field `:90`, `SessionInfo`, the capture sites in
  `_run_backend` `:866-905`). For Codex it holds a `thread_id`.
- The `_run_backend` recovery loop (`:825-966`) respawns with
  `"continue"` to work around a *Claude-CLI* bug
  (`docs/2026-05-18-bg-pipeline-hardening.md` §2); Codex must not inherit
  it.

---

## 4. Data model changes

### 4.1 New column `sessions.backend`

Make a session's backend explicit and durable, not inferred from its
credential (a Codex subscription credential is directory-backed and may
not look like a "secret" at all — §7).

- **Migration** in `Database._apply_migrations` (`server/database.py:151`),
  same wrapped-`ALTER` idempotent pattern as `credential_id`/`archived`:
  ```sql
  ALTER TABLE sessions ADD COLUMN backend TEXT NOT NULL DEFAULT 'claude-code'
  ```
  `DEFAULT 'claude-code'` backfills every existing row → no behavior
  change for current users. Add the column to the `CREATE TABLE sessions`
  block too.
- `Session.backend: str = "claude-code"` (`session_manager.py:83`).
- `CreateSessionRequest.backend: BackendKind = BackendKind.claude_code`
  (`models.py:15`); `SessionInfo.backend: BackendKind` (`models.py:29`).
- Thread through `create_session` / `import_session` (`session_manager.py:167,192`),
  `Database.save_session` (`database.py:255`), and `archive_session`
  (inherits backend alongside name/working_dir/credential_id).

### 4.2 Validation: credential backend must match session backend

On create, if `credential_id` is set, its `backend` must equal the
session `backend` — enforce a 400 in `server/routers/sessions.py`. A
Codex session must not run on a Claude credential.

### 4.3 Generalize `claude_session_id` → `backend_session_id`

The field now holds either a Claude session id or a Codex `thread_id`.
**Recommendation: rename end-to-end** (Octopus is pre-1.0, single-user;
the contract churn is worth the honesty). DB column (copy-forward in
`_apply_migrations`), `Session` field, `SessionInfo`,
`update_session_field`, the `_run_backend` capture sites. `octopus
pull` / `handoff` / `jsonl_writer.py` are inherently Claude-JSONL and
stay gated to `backend == "claude-code"`. Regenerate TS contracts.
(Fallback if we won't touch the API: keep the name, document it generic
— see §10.)

---

## 5. Backend changes

### 5.1 New module `server/backends/codex.py`

A `SubprocessJsonlBackend` subclass shaped exactly like
`claude_code.py`. The shared driver already gives subprocess lifecycle,
the 4 MiB stdout line cap, stderr buffering, graceful stop, and
`_which_with_fallback` (covers npm-global dirs). `CodexBackend`
implements `build_args`, `on_stdout_line`, and a no-op
`send_initial_prompt` (prompt is argv).

**`build_args` — verified flags** (off real `--help` + VM0's
`build_codex_args`). Argument order matters: `-C`/`--sandbox` are
**exec-level** and must precede the `resume` subcommand (resume's own
help accepts neither):

```
codex exec --json \
     --dangerously-bypass-approvals-and-sandbox \   # §5.6
     --skip-git-repo-check \
     -C <abs_working_dir> \
     -c developer_instructions="<octopus addendum, TOML-quoted>" \   # §5.4
     <MCP config: §5.3> \
     [-m <model>] \
     [resume <backend_session_id>] \
     -- <prompt>
```
- Resolve `working_dir` to absolute first (same reasoning as
  `claude_code.py:238`).
- New turn: `… -- <prompt>`. Resume turn: `… resume <id> -- <prompt>`
  (VM0 `command.rs` `build_codex_args`).
- **Env**: set `CODEX_HOME=<per-credential dir>` for the subscription
  login (§7) instead of `OPENAI_API_KEY`. Mirror `claude_code.py:330`'s
  `env = os.environ.copy()` then override.

### 5.2 Event normalization (`on_stdout_line`) — verified against VM0

One JSON object per stdout line. Map `type` → `BackendEvent`
(`server/backends/base.py:26`). This table is transcribed from VM0's
`codex-event-parser.ts` (then re-confirmed on a live run in Phase C):

| Codex `--json` event | → `BackendEvent` |
|---|---|
| `thread.started` `{thread_id}` | `session_started` (`session_id = thread_id`) — emit early so the resume id is captured before `result`, like `claude_code.py:376` |
| `turn.started` | ignore |
| `item.*` `{item:{id,type,…}}` | dispatch on `item.type` ↓ |
| &nbsp;&nbsp;`item.type == "agent_message"` (`text`) | `text` |
| &nbsp;&nbsp;`item.type == "reasoning"` (`text`) | `thinking` |
| &nbsp;&nbsp;`command_execution` (`item.started`, `command`) | `tool_use` (tool=`Bash`, `tool_use_id=item.id`, `input={command}`) |
| &nbsp;&nbsp;`command_execution` (`item.completed`) | `tool_result` (`content = aggregated_output ?? output`, `is_error = exit_code != 0`) |
| &nbsp;&nbsp;`file_edit`/`file_write`/`file_read` (`item.started`,`path`) | `tool_use` (Edit/Write/Read, `input={file_path}`) |
| &nbsp;&nbsp;`file_*` (`item.completed`) | `tool_result` (`content = diff` or done marker) |
| &nbsp;&nbsp;`file_change` `{changes:[{kind,path}]}` | `text` (summary) |
| `turn.completed` `{usage:{input_tokens,output_tokens,…}}` | `result` (`num_turns`/usage; **`cost=None`** — Codex reports tokens, not USD), then `self._close_stream()` (mirrors `claude_code.py:473`) |
| `turn.failed` `{error}` | `result` with `is_error=True` |
| `error` `{message,error}` | `error` (`is_error=True`) |

Lifecycle is identical to Claude (one invocation per turn, `result`
ends it, `_close_stream` releases the iterator) — **no
`SubprocessJsonlBackend` changes needed.**

### 5.3 MCP tool injection — Octopus-specific (VM0 gives no help here)

> **Post-implementation drift on the built-in set.** When this plan
> was written the set was `{viewer, bg, ask}`. Today it's
> `{bg, ask, ask_agent}`: `viewer` was removed when `/showme` became a
> client-driven REST resolver (the model shouldn't open files on its
> own — it can't tell whether anyone is at the screen), and `ask_agent`
> landed alongside the [`agent-collaboration.md`](agent-collaboration.md)
> feature. The injection *mechanism* described below stands unchanged;
> only the names in the set rotated.

`/showme`, `bg_run`, `ask_user` are MCP tools. Claude gets them via
`--mcp-config <inline JSON>` (`claude_code.py:264-317`) registering three
stdio servers (`python -m server.mcp_servers.{viewer,bg,ask}`) with a
callback env (`OCTOPUS_API_BASE`, `OCTOPUS_AUTH_TOKEN`,
`OCTOPUS_SESSION_ID`, `PYTHONPATH`; viewer also `OCTOPUS_WORKING_DIR`).

**VM0 registers no MCP servers into Codex**, so this is novel work built
on Codex's own MCP support (`codex mcp add`, and `[mcp_servers.<name>]`
in `config.toml`). Plan: **write a per-session `config.toml` inside the
managed `CODEX_HOME`** (which we already own for auth — nice synergy)
registering the identical three stdio servers + env:

```toml
[mcp_servers.viewer]
command = "<sys.executable>"
args = ["-m", "server.mcp_servers.viewer"]
env = { OCTOPUS_WORKING_DIR = "…", PYTHONPATH = "…" }
# bg, ask likewise with the callback env
```
This avoids the awkward `-c mcp_servers.bg.env.OCTOPUS_…=…` nested-table
override path. **Confirm in Phase C:** (a) `codex exec` honors
`config.toml` MCP servers, (b) the tool name it exposes to the model
(Claude shows `mcp__<server>__<tool>`; our `developer_instructions` text
must match Codex's scheme), (c) env reaches the server subprocess.

### 5.4 Instruction injection — solved: `-c developer_instructions`

Codex's analog of `--append-system-prompt` is
`-c developer_instructions="<text>"`, TOML-quoted (VM0
`command.rs:build_codex_developer_instructions_config`). No AGENTS.md or
instructions-file needed.

The text needs a **Codex variant**: the current `_OCTOPUS_SYSTEM_PROMPT`
bg-vs-Bash section is Claude-specific ("the Claude Code harness will
auto-background long Bash commands…") — false for Codex. Refactor
`_OCTOPUS_SYSTEM_PROMPT` (`claude_code.py:37`) into a **shared
tool-description core** + a **per-backend execution-model addendum**, so
the `/showme` / `ask_user` descriptions live once and only the shell/bg
paragraph diverges.

### 5.5 `_make_backend` dispatch (`session_manager.py:968`)

```python
def _make_backend(self, session: Session) -> BackendBase:
    if session.backend == "codex":
        return CodexBackend(session_id=session.id)
    if session.backend == "claude-code":
        return ClaudeCodeBackend(session_id=session.id)
    raise ValueError(f"Unknown backend: {session.backend!r}")
```

### 5.6 Headless sandbox + backend-agnostic recovery loop

- **Sandbox/approval.** VM0's guest runs `--sandbox danger-full-access`
  because it's *already* inside a microVM. Octopus runs on the user's
  own machine with no outer sandbox, so the Claude-parity choice is
  `--dangerously-bypass-approvals-and-sandbox` (the direct analog of
  Claude's `--dangerously-skip-permissions`, justified identically:
  Octopus is the only thing spawning these on the trusting user's
  behalf). Plus `--skip-git-repo-check` so non-repo working dirs run.
  Consequence: no approval round-trips → no `tool_approval_request`
  events → frontend approval path dormant for Codex, zero UI change.
- **Recovery loop.** Add `BackendBase.wants_premature_exit_recovery:
  bool = False` (`server/backends/base.py`). `ClaudeCodeBackend` sets it
  `True`; `CodexBackend` leaves it `False`. Gate the `_run_backend`
  recovery block (`:922-966`) on `backend.wants_premature_exit_recovery`
  so the loop stays free of backend names and Codex runs exactly once.

---

## 6. Frontend changes (`web/src/`)

### 6.1 Backend availability endpoint

`GET /api/backends` → `{"available": ["claude-code", "codex"?]}` via
`_which_with_fallback("codex")` (`subprocess_jsonl.py:37`). Codex appears
only when the binary resolves.

### 6.2 New-session form (`SessionList.tsx`)

- Backend radio (Claude / Codex), shown only when Codex is available;
  default Claude.
- The credential dropdown hardcodes
  `credentials.filter(c => c.backend === "claude-code")`
  (`SessionList.tsx:34`) — filter by the **selected** backend instead.
- Include `backend` in the create POST (`SessionList.tsx:55-63`).

### 6.3 Codex credential creation = a login, not a key field

A Codex credential is a logged-in `CODEX_HOME` (§7), so the
credential-create UI for Codex is a **login flow**, not an API-key text
box. Exact UI depends on the §7/§10 login-flow decision (device-auth vs
host login). OAuth start currently hardcodes `backend: "claude-code"`
(`CredentialList.tsx:104`).

### 6.4 Contracts & tool approval

`bun run generate:contracts` after the server schema lands (adds
`SessionInfo.backend`, `CreateSessionRequest.backend`, the resume-field
rename). `ToolApproval.tsx` / `ChatView.tsx` need no backend branch
(§5.6).

---

## 7. Auth — subscription via `CODEX_HOME` (the real open design)

**Confirmed mechanics:** `codex login` (interactive ChatGPT OAuth) or
`codex login --device-auth` (URL + code, no localhost redirect) writes
an `auth.json` with `auth_mode: "chatgpt"` into `$CODEX_HOME` (default
`~/.codex`); Codex refreshes that token itself. `CODEX_HOME` isolates it
(VM0 sets it per-run at `cli/mod.rs:140`; `--ignore-user-config` docs
confirm "auth still uses `CODEX_HOME`").

**What we borrow vs. drop from VM0:** borrow `CODEX_HOME` isolation and
the `auth_mode:"chatgpt"` subscription path; **drop** VM0's placeholder-
JWT fabrication + egress-firewall token swap (`codex_auth.rs`) — that's
a multi-tenant microVM mechanism with no analog in a single-user local
app, where a real on-disk login is simpler and correct.

**Octopus model:** a Codex credential = a **directory-backed** identity:
Octopus owns a per-credential dir (e.g. `~/.octopus/codex/<credential_id>/`)
used as `CODEX_HOME`; `codex login` writes `auth.json` there; session
spawn sets `CODEX_HOME` to it. **No secret is stored in Octopus's DB** —
Codex manages the token and its refresh inside that dir. This diverges
from the Claude credential (encrypted `sk-ant-` string in
`credential_secrets`); the `backend_credentials` row for Codex stores
metadata + the dir pointer, not a secret blob. Optional hardening
(later, like VM0's `codex-auth-json-parser.ts`): parse `auth.json` to
surface the account/plan and reject free-tier.

**The remaining product decision (§10):**
- **A — host login.** User runs `codex login` once on the host;
  Octopus inherits `~/.codex`. Zero credential UI. Simplest; fine if you
  only ever drive Octopus from the host.
- **B — in-app device-auth login.** Octopus runs `codex login
  --device-auth` against a fresh per-credential `CODEX_HOME` (in a PTY),
  scrapes the URL+code, shows them in the UI; the user completes it on
  any device. Fits Octopus's remote-control premise; reuses the
  per-credential-`CODEX_HOME` model and the credential UI. Recommended.

---

## 8. Implementation phases

- **A — Data model + dispatch (Claude unchanged).** §4 + §5.5 + §5.6
  recovery flag. Pure plumbing; existing tests stay green.
- **B — `codex.py` against a fake CLI.** `tests/_fixtures/fake_codex_cli.py`
  scripting the §5.2 event stream (now a *known* shape, transcribed from
  VM0) + `tests/test_backend_codex.py` for the normalizer.
- **C — Live confirmation (needs a logged-in subscription).** Log in,
  run a real turn, diff actual events against §5.2, confirm §5.3 MCP
  injection works and the tool name matches the instructions text, and
  write `docs/codex-protocol-notes.md` (sibling of `cli-protocol-notes.md`,
  recording codex 0.132.0). Build the chosen §7 login flow.
- **D — Frontend.** §6.
- **E — Real-CLI e2e**, gated on `which codex` like
  `tests/test_backend_claude_code_real.py`, plus a Playwright
  create-Codex-session spec asserting `/showme` opens the viewer (proves
  MCP injection end-to-end).

## 9. Tests (mirror the Claude layout)

- `tests/_fixtures/fake_codex_cli.py` — scripted `--json` stream from §5.2.
- `tests/test_backend_codex.py` — normalizer unit tests.
- `tests/test_backend_codex_real.py` — `skipif` no `codex`; asserts live
  event shapes still match the normalizer (keeps the fake honest).
- `session_manager` dispatch tests: `backend="codex"` → `CodexBackend`;
  recovery loop does **not** fire (`wants_premature_exit_recovery` False).
- DB migration test: existing rows backfill `claude-code`; resume-id
  rename copies forward.
- e2e: create Codex session, send a message, `/showme` opens the viewer.

## 10. Decisions to confirm before/at Phase A

1. **§7 login flow: host (A) vs in-app device-auth (B)?** Recommend B.
   Determines §6.3 + §7 build-out.
2. **Rename `claude_session_id` → `backend_session_id`?** Recommend yes
   (§4.3).
3. **Drop the old `claude_session_id` column post-migration, or keep one
   release for rollback?** Lean keep-one-release.
4. **MCP registration: generated `config.toml` (recommended, §5.3) vs
   `codex mcp add` vs `-c` overrides** — finalize after Phase C confirms
   `exec` honors `config.toml`.

## 11. VM0 reference — borrowed vs. not

**Borrowed (proven in VM0):** the `codex exec --json … -C … [resume <id>]
-- <prompt>` shape and exec-flags-before-`resume` ordering
(`command.rs:build_codex_args`); the full event→type mapping
(`codex-event-parser.ts`); `-c developer_instructions=<TOML>` for
instructions; `CODEX_HOME` for auth isolation; the `backend` enum +
dispatch pattern (`backends.ts`, `env.rs` `Framework`).

**Deliberately not borrowed:** placeholder-JWT + firewall token
injection (`codex_auth.rs`) — microVM multi-tenant only; `features.memories`;
`--sandbox danger-full-access` (we use full bypass since we're not in a
VM). **And the gap VM0 leaves us to fill:** MCP-tool injection into
Codex — VM0 does none, so §5.3 is ours to design.

## 12. Live-confirmation status (Phase C — done 2026-05-19)

Confirmed on a live, logged-in codex 0.132.0. Full record in
`docs/codex-protocol-notes.md`.

1. **Event schema drift** — ✅ confirmed, no drift. `thread.started.thread_id`,
   the `item.type` values, and the `turn.completed.usage` shape all match
   §5.2. One addition VM0's parser didn't cover: the **`mcp_tool_call`** item
   type — now handled by the normalizer (`server`+`tool` → `mcp__<server>__<tool>`).
2. **MCP injection** — ✅ confirmed, via **`-c mcp_servers.*` overrides** (not
   `config.toml` — the §5.3 decision; `-c` keeps per-session callback env while
   `CODEX_HOME` stays the stable auth dir). codex launched our viewer server,
   the model called it, and the per-server env reached the subprocess. The
   exposed tool maps to `mcp__<server>__<tool>`.
3. **`developer_instructions`** — ✅ lands; the model used a tool described only
   in the instructions.
4. **Login flow** — ⏳ still a product decision (§10 #1). Host `codex login`
   (option A) works today (`CODEX_HOME` unset → inherits `~/.codex`). In-app
   `--device-auth` (option B) is unbuilt.
