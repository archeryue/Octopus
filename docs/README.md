# Octopus docs

Three kinds of docs live here. **Start with `architecture.md`** for how the
system works today; the rest is design history and reference.

## Current

- **[architecture.md](architecture.md)** — the current system design: harness
  layer, agents, sessions, backends, MCP tools, connectors, bridges, the
  WebSocket protocol, the data model, and key decisions. The doc to read first.
- **[connectors-setup.md](connectors-setup.md)** — user how-to for setting up
  GitHub / Gmail / custom OAuth connectors entirely from the browser.

## Design records — [`plans/`](plans/)

One plan per major initiative, written before/while it was built and kept as the
design rationale. **Code comments cite these by filename + section** (e.g.
`agent-refactor.md §5.5`), so treat them as living references, not throwaways.

- **[plans/agent-refactor.md](plans/agent-refactor.md)** — first-class Agents.
- **[plans/codex-backend.md](plans/codex-backend.md)** — the Codex backend.
- **[plans/connectors.md](plans/connectors.md)** — the connector framework.
- **[plans/memory.md](plans/memory.md)** — per-agent native memory.
- **[plans/harness-layer.md](plans/harness-layer.md)** — the one-harness,
  profile-per-backend runtime boundary.
- **[plans/agent-collaboration.md](plans/agent-collaboration.md)** —
  agent-to-agent delegation (the `mcp__ask_agent__*` tools). Reverses
  the explicit "no A2A" carve-out in `agent-refactor.md` §40-41.

## Reference notes

Research captured while reverse-engineering the CLIs' stream protocols and a
post-mortem on a tricky pipeline. Cited from code where relevant.

- **[cli-protocol-notes.md](cli-protocol-notes.md)** — the Claude Code CLI's
  `--print` stream-JSON protocol.
- **[codex-protocol-notes.md](codex-protocol-notes.md)** — the Codex CLI's
  `exec --json` event schema.
- **[cli-system-prompt-notes.md](cli-system-prompt-notes.md)** — observed
  system-prompt behavior.
- **[post-mortems/](post-mortems/)** — incident write-ups (kept here because
  code/tests cite them inline). Currently
  [`2026-05-18-bg-pipeline-hardening.md`](post-mortems/2026-05-18-bg-pipeline-hardening.md):
  post-mortem on four `mcp__bg__run` → model-turn pipeline bugs. Note: the
  `server/backends/*` paths it cites predate the harness refactor — that code
  now lives under `server/harness/`.
