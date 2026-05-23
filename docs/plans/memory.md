# Agent Memory — Tech Plan

## 0. Why this exists, and how grounded this is now

This is the "north star" the Agent refactor was built toward
(`agent-refactor.md` §0, `future-features.md` "Deferred → Agent
memory"). The Agent is the durable entity that owns sessions and
schedules; memory is the durable *knowledge* that entity accrues.

The framing from the user: build a **model- and harness-agnostic**
memory. The memory is owned by Octopus (not by Claude Code's
`~/.claude/.../memory/` or Codex's `AGENTS.md`), stored as **Markdown
files**, and **both Claude Code and Codex can read it**.

Why "agnostic" is the whole point — and why native memory can't deliver
it: each harness has its own memory mechanism, and they don't share.
Claude Code auto-injects a per-user, per-*repo* memory dir
(`~/.claude/projects/<cwd-slug>/memory/`); Codex reads a per-repo
`AGENTS.md`. Neither is keyed to the Octopus *Agent*, and switching an
agent's backend from `claude-code` to `codex` (which Octopus now
supports per agent — `agent-refactor.md` §5.2, `codex-backend.md` §5.5)
would silently drop everything the agent "knew." An Octopus-owned,
agent-scoped store fixes both: one memory, keyed to the Agent, injected
and tool-exposed *identically* to whichever backend runs the turn.

Grounding — confirmed against the code, not assumed:

- **Both backends re-inject a system prompt every turn**, because the
  CLIs don't persist it across `--resume`. Claude Code:
  `--append-system-prompt` (`claude_code.py:365–386`). Codex:
  `-c developer_instructions=...` (`codex.py:208–250`). This is the
  hook for the *passive* read path (inject the memory index).
- **Both backends wire MCP servers from one backend-neutral entry**
  `{command, args, env}` (`connectors/base.py:105–123`), rendered as
  JSON `--mcp-config` for Claude (`claude_code.py:267–328`) and as
  `-c mcp_servers.<key>.*` TOML for Codex (`codex.py:142–206`). The
  three in-app servers (`viewer`, `bg`, `ask`) already ride this path.
  This is the hook for the *active* read/write path (a `memory` MCP
  server).
- **Octopus already owns on-disk state** under `~/.octopus/`
  (`config.py:14–24`: `attachments/`, `large-prompts/`, `codex/`). A
  `memory/` root slots in beside them with zero new infrastructure.
- **`_make_backend` already has the agent row** in hand
  (`session_manager.py:1150–1198`), so the agent id (hence the memory
  dir) is trivially available to pass to the backend.
- **The in-app MCP server template is tiny and file-direct.** `viewer`
  reads files straight from `OCTOPUS_WORKING_DIR` with a sandbox check
  (`mcp_servers/viewer.py`); `bg`/`ask` call back over HTTP only because
  they need host singletons. Memory is file-direct like `viewer`.

The proven reference design is *Claude Code's own memory*, which the
reader is literally seeing in this session's system reminder: a
`MEMORY.md` index loaded into context each turn + one Markdown file per
fact (frontmatter `name`/`description`/`metadata.type` + body) + a tool
to read/write/curate them. We reimplement that, Octopus-side and
agent-scoped, so it is byte-for-byte identical for Codex.

## 1. Goals

1. **Octopus-owned, agent-scoped Markdown memory.** One directory per
   agent under `~/.octopus/memory/<agent_id>/`, holding one `.md` file
   per fact (frontmatter + body), independent of any harness's native
   memory.
2. **Harness-agnostic read.** Every turn, regardless of backend, the
   agent sees what it knows. Two read paths, both backend-neutral:
   - **Passive:** the memory *index* is injected into the system prompt
     each turn (same channel as the tools/connector blurbs).
   - **Active:** a `memory` in-app MCP server exposes `memory_list` /
     `memory_read` / `memory_search` so the model can pull full content
     on demand. Identical tool names (`mcp__memory__*`) for both
     backends.
3. **Memory gets created and curated.** The same MCP server exposes
   `memory_write` / `memory_delete`; the model curates its own memory
   (model-driven, exactly like the reference design) under system-prompt
   guidance. Users can also view/edit/delete via the frontend.
4. **Survives a backend switch.** Flipping an agent claude-code↔codex
   preserves its memory with no migration — the same files, the same
   injection, the same tools.
5. **Per-agent toggle.** Memory is a built-in MCP server in the agent's
   `mcp_servers` set; an agent can run without it by dropping it, exactly
   like `viewer`/`bg`/`ask` today.
6. **No regressions, full test parity** with the rest of the codebase
   (CLAUDE.md "After Every Code Change").

## 2. Non-goals (v1)

- **Automatic memory extraction.** v1 is model-driven + user-driven: the
  model writes memories via the tool, users edit via the UI. A separate
  post-turn LLM pass that *mines* transcripts for facts is a distinct
  feature with its own cost/quality tradeoffs and a real product
  decision behind it; it is **not** a polish item being deferred — it is
  a different feature. Recorded in `future-features.md`, not started here.
- **Cross-agent / global shared memory.** v1 scopes memory to a single
  agent. A shared "org memory" pool is a deliberate future call
  (decision #5).
- **Semantic / vector search.** `memory_search` is substring + frontmatter
  match over the (small) file set. Embeddings are unwarranted at this
  scale and add an external dep.
- **Suppressing Claude Code's native auto-memory.** See open question
  §10.1 — we decide coexistence policy, we don't build a CLI patch.

## 3. Concepts & on-disk layout

```
~/.octopus/memory/<agent_id>/
    MEMORY.md                  # derived index — one line per fact
    user-prefers-tabs.md       # one fact per file
    deploy-runbook.md
    ...
```

A **memory file** mirrors the reference scheme exactly so the format is
familiar to the model and human-diffable:

```markdown
---
name: user-prefers-tabs
description: Indentation preference for this user's projects
metadata:
  type: user            # user | feedback | project | reference
---

The user wants tabs, not spaces, in all generated code.
Links to related memories with [[deploy-runbook]].
```

- **`name`** is the slug and the filename stem; kebab-case, validated
  (`^[a-z0-9][a-z0-9-]*$`, ≤ 64 chars, no separators / `..`). One fact
  per file.
- **`MEMORY.md` is derived, not authoritative.** It is regenerated by
  scanning every fact file's frontmatter (`- [Title](slug.md) — <description>`).
  Making it derived removes the incremental-edit race entirely: any
  writer rebuilds the whole index from the current file set, so two
  concurrent writers to *different* facts converge on a correct index.
  It is materialized to disk for human/UI viewing and for the harness to
  open via `memory_read("MEMORY")` if it wants, but the *injected* index
  is computed live from the same scan (so injection never depends on
  MEMORY.md being current).
- **Source of truth = the individual fact files.** Everything else
  (the injected index, the materialized MEMORY.md, the REST list) is a
  projection of them.

Why files, not a DB table: the user asked for Markdown files explicitly,
the content is naturally a document, and files give us free
human-inspectability + git-ability + a zero-migration story. The agent
id namespace already lives in the `agents` table; memory needs no new
table. (Considered and rejected: a `memories` table with a markdown
column — it buys nothing here and adds a migration + a sync surface.)

## 4. Storage layer — `server/agent_memory.py` (new)

A single module owns all file I/O so both the FastAPI process (injection
+ REST) and the MCP server process call the same code. Pure functions
over a base dir; no global state.

```python
@dataclass(frozen=True)
class MemoryFact:
    name: str            # slug / filename stem
    description: str
    type: str            # user | feedback | project | reference (free-form ok)
    body: str
    updated_at: float    # st_mtime, for sorting/UI

def memory_dir(agent_id: str) -> Path             # ~/.octopus/memory/<agent_id>, ~ expanded at call time
def list_facts(agent_id) -> list[MemoryFact]      # scan + parse frontmatter, sorted
def read_fact(agent_id, name) -> MemoryFact | None
def search_facts(agent_id, query) -> list[MemoryFact]   # case-insensitive substring over name/description/body
def write_fact(agent_id, name, description, type, body) -> MemoryFact   # validate slug, atomic write, rebuild index
def delete_fact(agent_id, name) -> bool           # unlink + rebuild index
def render_index(agent_id) -> str                 # the MEMORY.md text (also what we inject)
def rebuild_index(agent_id) -> None               # write MEMORY.md from current scan
```

Implementation notes (do-it-right details, not deferrals):

- **Atomic writes:** write to `name.md.tmp` then `os.replace` — no
  half-written file is ever readable. `rebuild_index` writes `MEMORY.md`
  the same way.
- **Concurrency:** a per-agent `filelock` (or `fcntl.flock` on a
  `.lock` file in the dir) guards `write_fact`/`delete_fact` so two MCP
  processes (two sessions of the same agent) serialize their
  index rebuilds. Reads are lock-free (atomic replace makes torn reads
  impossible). A single host is the only writer surface, so a file lock
  suffices — no DB row needed.
- **Slug safety:** reuse the spirit of `file_viewer.resolve_safe_path`
  — resolve `memory_dir(agent_id)/<name>.md` and assert it stays inside
  the agent's memory dir; reject anything that escapes. The MCP write
  tool is the untrusted caller here, so this is load-bearing.
- **Frontmatter parsing:** lightweight, dependency-free (split on the
  leading `---` fence, parse the small known key set). We control the
  writer, so we don't need a full YAML lib; malformed/hand-edited files
  degrade gracefully (missing description → empty string, still listed).
- **`~` expansion at call time**, mirroring `config.py`'s note so tests
  that monkeypatch `$HOME` see the override.

New setting in `config.py`:

```python
memory_dir: str = "~/.octopus/memory"   # per-agent subdir: <memory_dir>/<agent_id>/
```

**Agent deletion** must clean up the dir. `agent_manager` delete path
(and the archive→hard-delete path, if any) `shutil.rmtree`s
`memory_dir(agent_id)`. Archiving an agent keeps the files (history,
matching how archived sessions keep messages); only true delete removes
them.

## 5. Backend changes

### 5.1 Pass the agent id to the backend

Both backends already take `session_id`; add `agent_id` (and nothing
else — the dir is derived from it). `_make_backend`
(`session_manager.py:1150–1198`) passes `agent_id=agent["id"] if agent
else None` to both `ClaudeCodeBackend` and `CodexBackend`. When there is
no agent (legacy/tests), `agent_id` is `None` and memory is simply off.

### 5.2 The `memory` in-app MCP server (active read/write path)

New `server/mcp_servers/memory.py`, structured exactly like
`viewer.py` (file-direct, stdio FastMCP, sandboxed), spawned as
`python -m server.mcp_servers.memory` with env:

```
OCTOPUS_MEMORY_DIR   = <abs ~/.octopus/memory/<agent_id>>   # already agent-scoped
```

That single env var is all it needs — the dir *is* the agent scope, so
the server never trusts an agent id from the model. Tools (all delegate
straight to `server/agent_memory.py`):

| Tool | Signature | Returns |
|------|-----------|---------|
| `memory_list` | `()` | the index (names + descriptions) — the canonical fresh read |
| `memory_read` | `(name: str)` | full frontmatter + body of one fact (or a not-found message) |
| `memory_search` | `(query: str)` | matching facts (name + description + snippet) |
| `memory_write` | `(name, description, type, content)` | confirmation; creates/overwrites the fact + rebuilds index |
| `memory_delete` | `(name: str)` | confirmation or not-found |

The server is added to both backends' MCP entry-building exactly where
`viewer`/`bg`/`ask` are selected today (`claude_code.py:267–328`,
`codex.py:142–206`). It is gated on `agent_id is not None` **and** its
presence in the agent's `mcp_servers` set (so the per-agent toggle works
identically to the other three). When `agent_id` is `None`, the server
isn't wired at all.

### 5.3 Index injection (passive read path)

In each backend's prompt assembly — right where the connector blurb is
appended (`claude_code.py:354–363`, `codex.py:208–214`) — append a
**Memory** section built from two parts:

1. A static `_OCTOPUS_MEMORY_PROMPT` (shared by both backends; tool names
   are identical so no per-harness variant) that explains the file
   scheme and *when* to remember / recall / update / delete — adapted
   from the reference design's "# Memory" guidance. This is what makes
   the model actually curate memory instead of ignoring the tool.
2. The **live index** for this agent (`agent_memory.render_index`). If
   the agent has no memories yet, inject only a one-liner ("No memories
   recorded yet — use `memory_write` to record durable facts") so the
   model knows the capability exists without noise.

Both are appended to the same string already passed via
`--append-system-prompt` (Claude) / `developer_instructions` (Codex).
Because injection reads the files fresh on every turn, a memory the model
wrote on turn N is visible passively on turn N+1 with no extra plumbing.

Empty/disabled cases: if `memory` is not in the agent's MCP set, inject
nothing and don't wire the server (the agent opted out).

## 6. REST + WebSocket (host side)

Frontend needs to view/manage memory; the host reads the same files.

New router `server/routers/memory.py`, mounted under the agents tree:

- `GET    /api/agents/{agent_id}/memory` → `[MemoryFact]` (list)
- `GET    /api/agents/{agent_id}/memory/{name}` → one fact
- `PUT    /api/agents/{agent_id}/memory/{name}` → create/update
- `DELETE /api/agents/{agent_id}/memory/{name}` → delete

All four delegate to `server/agent_memory.py` (same code the MCP server
uses) under the same per-agent lock, so UI edits and model writes can't
corrupt the index. Auth: the existing bearer-token dependency, like
every other route.

**Live updates:** when a memory changes *during a session* (model called
`memory_write`/`memory_delete`), the user watching that session should
see it. The MCP server is file-direct (no host round-trip), so to emit a
WebSocket event we either (a) have the host detect the change, or (b)
give the memory MCP server an optional host-callback like `bg`. Decision
#4 picks between "no live push in v1 — the Memory panel refetches on
open / on turn-end" (simplest, fully correct) and "memory server posts a
tiny `memory_changed` ping to the host → WS broadcast" (live, matches
`bg`'s pattern). Recommendation: start with refetch-on-turn-end (the
session already emits a turn-complete event the panel can listen to);
add the ping only if live mid-turn updates prove worth it.

## 7. Frontend (`web/src/`)

A **Memory** panel in the agent settings/detail surface (where
`mcp_servers`, connectors, model, etc. already live):

- **List** facts (name, type badge, description, updated-at), sorted
  most-recent-first.
- **View/edit** a fact in a Markdown editor (frontmatter fields as form
  inputs: name, description, type; body as a textarea/markdown editor).
- **Create** / **delete** with confirm.
- A clear note that the agent curates this itself and that it's shared by
  *all* of the agent's sessions and survives a backend switch.
- The agent's `mcp_servers` toggle for `memory` lives with the existing
  built-in-server toggles; turning it off hides/disables the panel's
  "the agent can read this" affordance (files are kept).

Store wiring (zustand) mirrors connectors: a `memory` slice with
`fetchMemory(agentId)`, `saveMemoryFact`, `deleteMemoryFact`. Use
`useSessionStore.getState()` inside callbacks per CLAUDE.md conventions.

## 8. Implementation phases

- **Phase A — storage core.** `server/agent_memory.py` + `config.py`
  setting + unit tests (CRUD, slug validation, atomic write, index
  rebuild, concurrent-write lock, `$HOME` override). No backend wiring
  yet. Independently verifiable.
- **Phase B — MCP server.** `server/mcp_servers/memory.py` + tests
  driving the tools against a temp dir. Still not wired into a launch.
- **Phase C — backend wiring.** Pass `agent_id`; add `memory` to MCP
  selection in both backends; inject `_OCTOPUS_MEMORY_PROMPT` + live
  index. Tests assert `build_args()` for *both* backends includes the
  memory server entry and the injected index when the agent has facts,
  and omits it when the agent opts out / has no id. Add `memory` to the
  default `mcp_servers` set. A real-CLI test (gated on `claude` / `codex`
  on PATH, like the existing real-CLI suites) that seeds a fact and
  confirms the model can recall it.
- **Phase D — REST + frontend.** Router + agent-delete cleanup + Memory
  panel + store + unit/e2e tests. WS live-push only if decision #4 says so.
- **Phase E — docs + counts.** Update CLAUDE.md test-count table and the
  coverage matrix; move "Agent memory" out of `future-features.md`
  "Deferred"; note the native-memory coexistence decision (§10.1).

Phases A→B→C are the agnostic-read/write spine and are independently
testable; D is the human surface; each leaves the suite green.

## 9. Tests

Mirror the existing layout (CLAUDE.md "Test Coverage"):

- **Backend unit (pytest):** `agent_memory` CRUD + index + locking +
  slug safety; the `memory` MCP tools; `build_args()` injection &
  server-wiring for **both** backends (present/absent/opted-out/no-agent);
  REST routes (list/get/put/delete, 404s, auth); agent-delete rmtree;
  real-CLI recall test (gated on PATH).
- **Frontend unit (vitest):** memory store slice; Memory panel render.
- **E2E (Playwright):** open an agent's Memory panel, create/edit/delete
  a fact, confirm it lists; (optional, real-CLI) a turn where the model
  reads an injected memory.

All suites green before commit — zero failures, no skips dressed as
"flaky" (CLAUDE.md).

## 10. Open questions / decisions to confirm before Phase A

1. **Coexistence with Claude Code's native auto-memory.** When Octopus
   runs `claude --print` in a repo, the CLI also injects its own
   per-repo memory dir (this session's system reminder *is* that). For a
   claude-code agent we'd then have two stores. Options: (a) leave it —
   they're separate dirs and don't corrupt each other, Octopus memory is
   simply the cross-harness one; (b) investigate a CLI flag/setting/env
   to disable native auto-memory so Octopus's is authoritative. We should
   **investigate (b)'s feasibility** before deciding; default to (a) if
   no clean switch exists. (We do *not* hand-patch the CLI.)
2. **Scope: per-agent (recommended) vs per-session vs global.** Plan
   assumes per-agent — it's the durable entity and the refactor's stated
   north star. Confirm.
3. **Who writes, v1: model-driven (recommended) vs auto-extraction.**
   Plan assumes model-driven via the tool + user via UI (matches the
   reference design, no second LLM pass). Auto-extraction is a separate
   future feature, not a deferral of this one. Confirm.
4. **Live mid-turn UI updates.** Refetch-on-turn-end (simple, correct)
   vs a `bg`-style host-callback ping for live push. Recommendation:
   refetch first, add ping only if needed.
5. **Shared/global memory pool** across an org's agents — future call;
   confirm it's out of v1.
6. **Memory in the default agent's MCP set.** Plan adds `memory` to the
   default `["ask","bg","viewer"]`. Confirm we want it on by default
   (recommended — it's the headline feature) vs opt-in per agent.
