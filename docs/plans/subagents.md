# Sub-agents in Octopus — capture & open decision (DEFERRED)

Status: **deferred** — recorded now, to be solved later. No code yet.

## 1. The facts (verified against the live CLIs)

- **Claude Code has first-class in-turn sub-agents.** The `Task` tool + agent
  definitions in `~/.claude/agents/*.md`; a turn spawns them in an isolated
  context window and collects their result. The `/deep-research` skill fans out
  ~75 of these — and that uncontrolled fan-out (plus no process-group reaping
  and no turn timeout) is what hung session "stock".
- **Codex has no in-turn sub-agent primitive.** Codex 0.132's commands are
  `exec / review / mcp / plugin / fork / resume / cloud / …` — there is no
  `Task`/`subagent`/`delegate` tool. Its multi-agent story is **Codex Cloud**
  (remote async tasks), not a nested in-turn subagent.

So "both have sub-agents" is only half true, and the two are not symmetric.

## 2. Octopus already has the harness-agnostic sub-agent: delegations

`mcp__ask_agent__ask` (agent-collaboration.md) spawns a child session under
another Octopus agent — possibly a different backend (claude→codex is tested) —
runs a real turn, and injects the reply back. Versus the harnesses' native
subagents it adds exactly what they lack: a real per-subagent session
(visible, persisted, cancellable), cost attribution, depth/cycle guards, and
cross-backend composition. This is the sub-agent abstraction, done agnostically.

## 3. What "support sub-agents" could mean — three things

1. **Cross-agent sub-agents** → already delegations. Enrich here (parallel
   fan-out, a reusable sub-agent/preset library, bounded concurrency).
2. **In-turn context isolation** ("explore the repo in a scratch context so the
   main turn stays clean") — the legitimate use native subagents serve that
   delegation doesn't cover cleanly. The native-deep-research design's bounded,
   throwaway **scoped sub-turn** is Octopus's controlled version of this and
   generalizes into a small harness-agnostic "spawn a bounded sub-turn"
   primitive (works for codex too, which has no native Task).
3. **Native harness subagents** (Claude `Task`) running inside an Octopus turn —
   allowed today (we don't deny `Task`), but unbounded, opaque (no session/UI/
   cost/cancel), Claude-only. The hang vector.

## 4. The open decision (to settle later)

For native Claude `Task` inside an Octopus turn:
- **(a) keep-but-bound** — leave `Task` allowed, rely on Layer-1 (turn-safety:
  process-group reaping + idle/overall timeout) to bound and reap it. Preserves
  a useful Claude capability; least disruptive. *(current lean)*
- **(b) disable + route** — add `Task` to the default `tool_deny`, and steer all
  sub-agent work through Octopus delegations + the bounded sub-turn primitive.
  Fully agnostic and observable; loses Claude's in-context subagents.

Recommendation to revisit: **(a)** first (Layer 1 makes it safe regardless),
and treat delegations as the canonical sub-agent abstraction to invest in.
Note: Layer 1 (turn-safety.md) makes (a) viable *and* underpins
native-deep-research.md, so it lands first either way — this decision can wait.

## 5. Why deferred

Layer 1 + native deep research are the active builds and don't depend on
resolving this. Native `Task` is already bounded once Layer 1 ships, so there's
no urgency to pick (a) vs (b) now. Revisit after Layer 1, with real usage data
on whether native `Task` causes further trouble.
