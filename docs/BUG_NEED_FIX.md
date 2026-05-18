# Known Bugs To Fix

Bugs we've discovered but deferred. Each entry should list the symptom,
where it surfaces, what we know about the cause, and what a fix
probably looks like.

---

*No open bugs at the moment.*

Past entries are kept for the historical record (handy when the same
class of bug shows up again).

---

## (resolved) 1. `can_use_tool` control_response format out of date for Claude Code 2.x

**Status**: FIXED in commit
[`(see commit log for "behavior/updatedInput")`](#). Backend now sends
`{"behavior": "allow", "updatedInput": …}` /
`{"behavior": "deny", "message": …}` instead of the legacy
`{"allow": …, "reason": …}`. Real-CLI tests run clean with a tripwire
that fails on any `ZodError` or `Tool permission request failed` text
in tool_results or stderr.

The old shape happened to work for normal tools because the CLI
tolerated whichever schema variant matched first, but `AskUserQuestion`
triggered the strict modern validator and the union check failed.

## (resolved) 2. Tailwind padding/margin utilities silently no-op'd

**Status**: FIXED in commit
[`(see "unlayered universal reset")`](#). `index.css` had a universal
`* { padding: 0; margin: 0; }` reset outside any `@layer`. CSS cascade
layer rules put unlayered styles above all layered ones, so every
Tailwind `@layer`-scoped `.px-N` / `.py-N` / `.m-N` / `.gap-N` lost
the cascade against that one rule. Spacing utilities computed to 0px
everywhere.

Fix: wrap the reset in `@layer base` so it sits inside the same
cascade-layer space the utilities live in.

This one cost us a full evening of pushing spacing classes that never
applied. Lesson lives in the `verify-first` memory: for CSS changes,
verify with `getComputedStyle` from a real browser before claiming
done.
