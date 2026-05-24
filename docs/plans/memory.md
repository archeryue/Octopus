# Agent Memory — Tech Plan (native, agent-written, per-agent)

## 0. What we're building, and why this shape

The Agent is the durable entity that owns sessions and schedules
(`agent-refactor.md`). **Memory is the durable knowledge that entity
accrues**, and it must survive across sessions and — as much as
possible — across a backend switch.

This plan supersedes the earlier "Octopus-owned memory store" design
(DB rows + a dedicated memory MCP server + injection of the index every
turn). That approach was built and then **reverted** on the user's
decision. The reason it was rejected:

> If we don't use the native memory, it raises a new risk: the harness
> will *sometimes* use its own native memory anyway. Claude Code's
> native memory is always on; running a second, Octopus-owned store next
> to it means two memory systems fighting. Use the native memory.
> Changing an agent's harness is a low-frequency operation, so we accept
> that the memory format may be mixed on a backend switch.

So the design is: **use each harness's native, agent-written file
memory**, persist and scope it **per agent**, and point both harnesses at
**one shared per-agent markdown directory** so the memory is genuinely
harness-agnostic on disk.

### How the user's "let the agent write the file itself" lands

Claude Code's native memory *is already* "the agent writes markdown
files": a `MEMORY.md` index plus frontmatter `*.md` fact files under
`$CLAUDE_CONFIG_DIR/projects/<cwd-slug>/memory/`, maintained by the agent
via its built-in memory tool, auto-injected at session start. Codex has
full file tools, so it can do the *exact same thing* against the *same*
directory when instructed to. We therefore make Codex match Claude
rather than the reverse — one model, two harnesses, no second store, no
async-consolidation dependency.

## 1. Empirical grounding (verified in real CLI runtimes, not assumed)

Everything below was confirmed by running the real `claude` / `codex`
CLIs headlessly (the same mode Octopus spawns), not against fakes:

- **Claude reads native memory in `--print`.** Seeded a project's
  `memory/MEMORY.md` + a fact file; `claude --print` recalled the facts.
- **Claude writes native memory in `--print`.** Told to remember a
  durable fact, it created a new frontmatter `*.md` and added a
  one-line pointer to `MEMORY.md`, unprompted as to format.
- **Per-agent `CLAUDE_CONFIG_DIR` + symlink + copied auth works.** With
  `CLAUDE_CONFIG_DIR=<agent>/claude-home`, a copied `.credentials.json`
  (no env token), and `claude-home/projects/<cwd-slug>/memory` symlinked
  to a canonical per-agent dir, Claude both **read** seeded memory and
  **wrote** new memory *through the symlink* into the canonical dir.
- **Codex reads `MEMORY.md` from a persistent `CODEX_HOME`.** With
  `features.memories=true` + a populated `memories/MEMORY.md`, a fresh
  `codex exec` located and read it (it knew the path from `use_memories`)
  and answered correctly. Codex writing files via its tools is a given.
- **Codex's *native* auto-memory pipeline is NOT used.** Its Phase-2
  consolidation is a background agent that needs a long-lived session;
  it does not complete inside a short `codex exec` (verified: capture
  works, but `MEMORY.md`/`raw_memories.md` stay empty after seconds-long
  exec turns even with idle gates zeroed). We sidestep it entirely: Codex
  memory is a plain directory it reads/writes with file tools, driven by
  `developer_instructions`. `features.memories` stays **off** — no dual
  system, no consolidation wait, no format surprises.

## 2. On-disk layout

```
<agents_dir>/<agent_id>/
   memory/                 # CANONICAL per-agent memory (markdown)
      MEMORY.md            #   index: "- [Title](file.md) — hook"
      <topic>.md           #   frontmatter fact files
   claude-home/            # per-agent CLAUDE_CONFIG_DIR
      .credentials.json    #   (only when no env-token credential; copied from host)
      projects/<cwd-slug>/memory -> ../../../../memory   # symlink to canonical
```

`agents_dir` defaults to `~/.octopus/agents` (new setting). The canonical
`memory/` dir is the single source of truth; Claude reaches it via the
symlink, Codex reaches it directly by absolute path.

`<cwd-slug>` is Claude Code's project-path encoding of the session's
absolute working directory (leading `/` dropped, path separators and
other special chars → `-`; pinned to the empirically-observed rule —
see §6). One symlink is ensured per (agent × working-dir); all of an
agent's working dirs point their Claude memory at the agent's single
canonical `memory/` dir, so memory does not fragment by project.

## 3. Per-harness wiring

**Claude (`claude_code.py`):**
- `build_turn_argv` sets `env["CLAUDE_CONFIG_DIR"] = ctx.agent_config_dir`
  (pure; testable). Auth still comes from the env token when a credential
  is attached (`ANTHROPIC_API_KEY`/`CLAUDE_CODE_OAUTH_TOKEN` override the
  config dir), so a per-agent config dir is safe.
- A `prepare_workspace(ctx)` profile hook (run in `HarnessRun.start()`,
  *not* in `build_turn_argv`, so argv inspection has no FS side-effects)
  ensures `claude-home/` exists, ensures the
  `projects/<cwd-slug>/memory` symlink → canonical `memory/`, and — only
  when no env token is present — copies the host
  `~/.claude/.credentials.json` in once (best-effort host-login fallback).
- No extra system-prompt text: Claude's native memory auto-activates and
  auto-injects `MEMORY.md`.

**Codex (`codex.py`):**
- `CODEX_HOME` is unchanged (stays the per-credential auth dir) — memory
  is fully decoupled from auth, so there is no per-agent auth-sync hazard.
- A memory blurb is appended to `developer_instructions` naming the
  absolute canonical `memory/` path and the read-at-start /
  write-durable-facts / maintain-`MEMORY.md` protocol, in Claude's
  format. Codex uses its normal file tools (`--dangerously-bypass-
  approvals-and-sandbox` already grants out-of-cwd file access).
- `features.memories` is **not** enabled.

## 4. Plumbing

- `RunConfig` gains `memory_dir: str | None` and
  `agent_config_dir: str | None`. `TurnContext` gains the same two.
- `session_manager._run_config` computes both from `session.agent_id`
  (via `config.agents_dir`) and defensively ensures the canonical dir
  exists.
- `RuntimeProfile` gains optional
  `prepare_workspace: Callable[[TurnContext], None] | None`. Claude sets
  it; Codex leaves it `None`. `HarnessRun.start()` calls it before spawn.
- The memory blurb is composed in `assembly.py` (single-point, like the
  connectors blurb), gated by a profile flag (`injects_memory_prompt`) so
  only Codex receives it; Claude relies on native injection.
- `agent_manager.create_agent` provisions `<agent>/memory/` +
  `<agent>/claude-home/`; agent delete removes the tree.

## 5. Accepted trade-offs (explicit)

- **Backend switch may mix formats.** Claude's and Codex's edits share
  one markdown dir; a switch reads the other's prior writes. Low
  frequency, accepted per the user.
- **Host-login (no-credential) Claude agents** get a *copied*
  `.credentials.json`; an OAuth refresh inside the agent dir won't
  propagate back to the host file. Single-user tool, low frequency,
  documented. Credentialed agents (the normal case) use the env token
  and are unaffected.

## 6. Verification plan

- **Unit:** canonical/claude-home path derivation; `<cwd-slug>` encoding
  (incl. dots/underscores, pinned to the empirically-observed rule);
  symlink idempotency; Claude `build_turn_argv` sets `CLAUDE_CONFIG_DIR`;
  Codex `developer_instructions` contain the memory blurb + path; Codex
  does **not** enable `features.memories`; agent create provisions dirs,
  delete removes them.
- **Real-CLI (gated on `claude`/`codex` on PATH, like the existing
  real-CLI suites):** write a fact in session A, read it back in a fresh
  session B against the same agent — once per harness — proving the full
  agent-written persist→recall cycle end to end.

## 7. Implementation phases

- **Phase A — paths + provisioning core** (`server/agent_memory.py`, new,
  small): `agents_dir` setting; `agent_dir`/`canonical_memory_dir`/
  `claude_config_dir` derivation; `cwd_slug(working_dir)`; `ensure_dirs`;
  `ensure_claude_symlink`; `ensure_claude_auth`. Pure functions + unit
  tests (incl. `$HOME` override + the encoding rule).
- **Phase B — plumbing**: `RunConfig`/`TurnContext` fields;
  `RuntimeProfile.prepare_workspace`; `HarnessRun.start` calls it;
  `assembly` memory-blurb gated by `injects_memory_prompt`.
- **Phase C — profiles**: Claude sets `CLAUDE_CONFIG_DIR` +
  `prepare_workspace`; Codex gets the blurb. Unit tests on both argvs.
- **Phase D — session/agent wiring**: `_run_config` computes the dirs;
  `agent_manager` provisions on create, cleans on delete. Real-CLI cycle
  test.
- **Phase E — docs + counts**: CLAUDE.md test table + coverage matrix.

Each phase leaves the suite green (CLAUDE.md "After Every Code Change").
