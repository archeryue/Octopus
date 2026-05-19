# What lives in the CLI system prompt vs in user/project memory

The `claude` CLI runs a fresh process per turn. It does **not** see
user-scoped auto-memory or project `CLAUDE.md` files unless we hand
them to it explicitly. Three different layers carry guidance into
the model, with different scopes and reload semantics:

| Layer | How it reaches the model | Scope | When to use |
|---|---|---|---|
| `--append-system-prompt` (CLI argv) | Octopus's `claude_code.py` sets it on every spawn (`_OCTOPUS_SYSTEM_PROMPT`) | Every CLI invocation Octopus makes, for every user, every session | Rules about how to use Octopus's *own* tools (`mcp__bg__run`, `mcp__viewer__show_file`, `mcp__ask__user`); behaviors the agent must follow regardless of which human is driving |
| Auto-memory (`~/.claude/projects/<repo>/memory/`) | Loaded by the harness as conversation context | Per-user, per-repo. A teammate cloning the repo starts with empty memory | Personal preferences, feedback corrections, things the *user* discovered they want the agent to remember |
| `CLAUDE.md` (in the repo) | Loaded by the harness, checked into git | Per-repo, per-clone | Project conventions: commands, test layout, conventions everyone working on this repo should know |

## How `--append-system-prompt` is wired

`server/backends/claude_code.py` builds a long Python string
(`_OCTOPUS_SYSTEM_PROMPT`) describing the three MCP tools Octopus
injects (`viewer`, `bg`, `ask`), and how to use each. The string is
then passed as a positional argv element on every CLI spawn:

```python
argv = [
    self.binary,
    "--print",
    ...
    "--append-system-prompt", _OCTOPUS_SYSTEM_PROMPT,
    ...
]
```

VM0 uses the same hook (`vm0/crates/guest-agent/src/cli/command.rs`
around line 52, reading from `VM0_APPEND_SYSTEM_PROMPT` env var) —
this is the canonical path for an outer controller to teach the
model about controller-specific tools.

## Why the bg-vs-Bash rule moved here

It used to live only as a `feedback` memory in my auto-memory. That
covered me — but a fresh user opening Octopus would not see the
rule, so the model would happily reach for Bash on a test suite and
hit the auto-backgrounding-then-killed trap we kept stepping on.
Moving the rule into `--append-system-prompt` means:

- Every spawn of the `claude` CLI from Octopus carries the rule.
- A new user / new machine gets the same behavior on day one.
- The auto-memory copy is now redundant for *new* sessions, but
  remains valid as a per-user reinforcement.

The actual text of the rule (see `_OCTOPUS_SYSTEM_PROMPT` in
`server/backends/claude_code.py`) is intentionally strict: "use
bg_run unconditionally for any test suite / build / install /
sleep / network fetch". That's framed as a bright line because
"≥30s use bg_run, <30s use Bash" — the prior wording — was too
permissive and let the model fall back to Bash for anything it
thought would be quick.

## When to add new rules here

A candidate belongs in `--append-system-prompt` when it is:

- **Universal**: applies to every user driving Octopus, not just
  one person's preference.
- **About Octopus's own tools or runtime quirks**, not about a
  specific project's code or commands.
- **A safety/correctness invariant**, not a stylistic preference.

If any of those three fail, it probably belongs in auto-memory
(per-user) or `CLAUDE.md` (per-repo), not here.
