# Future features / open work

Per the project rule (`CLAUDE.md`), this file is **not** a parking lot for
"do it later". It lists work we've *genuinely chosen to defer* — because it
needs a real second use case, an external dependency, or a product decision —
not half-finished features. If something is worth doing, it gets done in the
session that starts it.

## What already shipped

The founding initiatives are all built and live-verified; their full design
records live in [`plans/`](plans/), and the current system is described in
[`architecture.md`](architecture.md):

- **First-class Agents** — durable assistant definitions that own sessions,
  schedules, and bridge bindings. ([`plans/agent-refactor.md`](plans/agent-refactor.md))
- **Codex backend** — a second backend beside Claude Code, driven by the user's
  ChatGPT subscription; selectable per session, with in-app device-auth login.
  ([`plans/codex-backend.md`](plans/codex-backend.md))
- **Connectors** — agent-scoped, browser-only OAuth access to GitHub, Gmail, and
  user-defined custom OAuth2 APIs as MCP tools. ([`plans/connectors.md`](plans/connectors.md))
- **Agent memory** — one canonical per-agent markdown dir shared by both
  backends (the north star behind the Agent refactor). ([`plans/memory.md`](plans/memory.md))
- **Harness layer** — the single, profile-driven boundary for all model/runtime
  interaction. ([`plans/harness-layer.md`](plans/harness-layer.md))
- **Agent-to-agent collaboration** — the `mcp__ask_agent__*` MCP tools.
  One agent can delegate to another by name; the delegated child runs
  in its own session under its own agent's config; replies and
  questions travel one hop, to the caller. Follow-up rounds pass the
  prior `delegation_id` back to `ask`, reusing the same child session
  so the delegated agent keeps her transcript. Reverses the explicit
  "no A2A" carve-out in `agent-refactor.md` §40-41; no Run table was
  needed (a delegation is a normal `Session` row with
  `parent_session_id` set + `origin='delegation'`).
  ([`plans/agent-collaboration.md`](plans/agent-collaboration.md))

## Deferred — would need a concrete trigger

These are intentionally *not* started. Each notes what would justify doing it.

- **More bridge platforms (Discord, Slack, …).** The `Bridge` ABC is built to be
  extensible and Telegram exercises the full contract (binding, quiet mode,
  approval buttons, session switching). A second platform is a focused subclass —
  deferred until there's a real need for one, since each adds an API surface to
  maintain.
- **Richer notifiers.** `server/notifiers/` ships a `webhook` target on a
  pluggable base; `email` and browser-`push` destinations, and event types
  beyond `session_idle` (e.g. `question_pending`, `schedule_failed`), are
  sketched in-code but deferred until there's a destination someone actually
  wants wired up.
- **More built-in connectors.** GitHub and Gmail cover the current need, and the
  generic **custom** connector already handles any OAuth2 API from the browser.
  A new *built-in* (typed tools + identity lookup) is only worth it for an API
  used often enough to deserve first-class tools rather than the generic
  `request` tool. (Notion was explicitly dropped — the user moved to Obsidian,
  i.e. local Markdown the agent already edits directly.)

When one of these gets a real trigger, write its plan in `plans/` and build it
to completion.
