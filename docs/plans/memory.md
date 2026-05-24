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
- **Claude has a dedicated memory-dir override** —
  `CLAUDE_COWORK_MEMORY_PATH_OVERRIDE` (env) / the `autoMemoryDirectory`
  setting. Verified: with it set to a per-agent dir and `CLAUDE_CONFIG_DIR`
  left at the host default, `claude --print` read seeded memory from the
  override dir **and** the session transcript still landed in
  `~/.claude/projects/<cwd-slug>/` (so `--resume` is unaffected).
- **Do NOT relocate `CLAUDE_CONFIG_DIR`.** (Lesson learned the hard way: an
  earlier version moved it per-agent, which orphaned every session's resume
  transcript — `projects/<cwd-slug>/*.jsonl` lives under the config dir — and
  killed live sessions. The override relocates *only* the memory dir.)
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
```

`agents_dir` defaults to `~/.octopus/agents` (new setting). The canonical
`memory/` dir is the single source of truth; both harnesses point at it by
absolute path. Nothing else lives under the agent dir — memory is decoupled
from each harness's config/auth dirs, so `CLAUDE_CONFIG_DIR` and `CODEX_HOME`
are never touched.

## 3. Per-harness wiring

**Claude (`claude_code.py`):**
- `build_turn_argv` sets
  `env["CLAUDE_COWORK_MEMORY_PATH_OVERRIDE"] = ctx.memory_dir` (when an agent
  owns the session). This relocates *only* Claude's auto-memory dir to the
  canonical per-agent dir. `CLAUDE_CONFIG_DIR` is **left at the host default**,
  so session transcripts (`projects/<cwd-slug>/*.jsonl`, what `--resume`
  reads) and auth stay where they are. No symlink, no auth copy, no cwd-slug.
- No system-prompt text: Claude's native memory auto-activates and
  auto-injects `MEMORY.md` from the override dir.

**Codex (`codex.py`):**
- `CODEX_HOME` is unchanged (per-credential auth dir).
- A memory blurb is appended to `developer_instructions` naming the absolute
  canonical `memory/` path and the read-at-start / write-durable-facts /
  maintain-`MEMORY.md` protocol, in Claude's format. Codex uses its normal
  file tools (`--dangerously-bypass-approvals-and-sandbox` grants out-of-cwd
  access).
- `features.memories` is **not** enabled (its consolidation doesn't run in
  short `codex exec` turns — see §1).

## 4. Plumbing

- `RunConfig` and `TurnContext` carry one field: `memory_dir: str | None`.
- `session_manager._run_config` computes it from `session.agent_id` (via
  `config.agents_dir`) and ensures the dir exists. (Working dirs are frozen to
  absolute at session creation — `resolve_working_dir` — so a session's
  storage never shifts with the server's runtime cwd.)
- The memory blurb is composed in `assembly.py` (single-point, like the
  connectors blurb), gated by `RuntimeProfile.injects_memory_prompt` so only
  Codex receives it; Claude relies on native injection.
- `agent_manager.create_agent` provisions `<agent>/memory/`; agent delete
  removes the tree.

## 5. Accepted trade-offs (explicit)

- **Backend switch may mix formats.** Claude's and Codex's edits share one
  markdown dir; a switch reads the other's prior writes. Low frequency,
  accepted per the user.

(No auth or resume trade-offs: memory never touches `CLAUDE_CONFIG_DIR` /
`CODEX_HOME`, so both harnesses authenticate and resume exactly as before.)

## 6. Verification

- **Unit:** per-agent path derivation + `$HOME`/settings override; idempotent
  provisioning; agent create/delete provisions/removes the dir; Claude
  `build_turn_argv` sets `CLAUDE_COWORK_MEMORY_PATH_OVERRIDE` and **never**
  `CLAUDE_CONFIG_DIR` (resume regression guard); Codex `developer_instructions`
  carry the blurb + path and **never** enable `features.memories`.
- **Real-CLI (gated on `claude`/`codex` on PATH):** each harness reads its
  seeded per-agent memory; **and** a Claude session that `--resume`s across two
  processes with the memory override on still recalls the prior turn (proving
  transcripts survived — the regression guard for the original incident).
