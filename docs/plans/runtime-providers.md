# Runtime providers (`runtime-1.0`)

> **Status:** in-progress — Design: the execution layer becomes an interface, with `LocalRuntime` as the default and providers living out of tree.

> The execution layer becomes an interface with two implementations — the
> local one Octopus has always had, and whatever the cloud version brings
> (the first being [Octopus-Runtime](https://github.com/archeryue/Octopus-Runtime)).
> Nothing is built yet.

[`multi-tenancy.md`](multi-tenancy.md) §2 chose the lowest rung of isolation on
purpose, and recorded what replaces it: *"C — Container or VM per tenant — real
— **the cloud version**"*. §6 marked `users.extra_roots` as "a single-box
affordance the cloud version drops". This is that work, and it starts by naming
the seam rather than crossing it.

The whole of Octopus assumes one thing about execution: that a child process
runs on this box, in a directory this process can `open()`. That assumption is
not spread thin — it is concentrated in **seven call sites and one path
module**. Making it an interface is therefore a small, bounded refactor; what
is *not* small is everything that becomes possible afterwards, which is why
this document draws the line carefully and defers most of it.

---

## 1. What this is, and what it is not

**It is**: a `Runtime` interface, a `LocalRuntime` that is the default and is
byte-for-byte the behaviour Octopus has today, and a registry that picks one.

**It is not a sandbox.** `LocalRuntime` isolates nothing, exactly as today.
This change does not improve the trust boundary by itself; it makes a boundary
*possible* by giving a second implementation somewhere to exist.

**It is not a remote-execution feature.** No provider other than
`LocalRuntime` ships here. A one-implementation interface is a fiction that
leaks local assumptions, so the contract below is written against a second
implementation that is known but absent — and §10 holds the first version to a
pure refactor precisely so the fiction stays cheap if the second one changes
shape.

**Providers live outside this repository.** `LocalRuntime` is the only one that
ships in-tree, and that is the intended end state rather than a starting
condition: a provider is a separate project on its own release cadence, written
by someone who cannot read this codebase's private assumptions. Two consequences
run through everything below — the protocol is **versioned and stable**, not an
internal detail refactored at will, and the conformance suite (§10) is the
contract rather than the documentation. An interface whose only other
implementation lives next door would not need either, which is exactly why
designing for that case produces a worse interface.

**It does not fix the server-side gaps.** The ownership and fan-out holes in
[`multi-tenancy-gaps.md`](multi-tenancy-gaps.md) — the WebSocket acting on any
session, unchecked ids on write, the silent fall back to the operator's login —
live in the server, on the far side of this seam. A tenant in its own VM is
still reachable through them. They are a prerequisite for any provider that
hosts other people, not something a provider makes go away.

**It must never make the single-box install worse.** Octopus runs on your own
laptop against your own subscription; that is the product. `LocalRuntime` is a
first-class implementation and the default, not a degraded fallback. Any
contract clause that is awkward for local execution is a bug in the contract.

Concretely, three rules hold for every version of the protocol:

* **Octopus alone runs every feature.** With `LocalRuntime` and nothing else
  installed — no provider package, no cluster, no VM — every feature works:
  turns, background tasks, Applications and their backends, `/rewind`, fork,
  research, schedules, accounts. `LocalRuntime` implements the *whole*
  protocol on this box's processes and filesystem; what it does not provide is
  isolation between accounts, and it says so (`multi-tenancy.md` §2, level A).
* **Every protocol addition lands in `LocalRuntime` in the same change.** File
  operations (`runtime-1.1`, §9), an `ensure` that can report a runtime
  offline, session history kept in the runtime — none of these exists until
  `LocalRuntime` implements it and Octopus's own suites pass on it. A method
  that only a remote provider implements is a feature the single-box install
  silently lacks.
* **The conformance suite has two tiers.** *Functional* cases — exec shapes,
  signalling, ports, callbacks, the complete-environment rule, confinement,
  state surviving `release` — bind every provider, `LocalRuntime` included.
  *Isolation* cases — one tenant cannot read another's files, processes or
  network — bind only a provider that claims isolation. `LocalRuntime` claims
  none, so it is held to the first tier in full and is not a failure of the
  second.

---

## 2. The seam, as it actually exists

Seven places spawn a process. They are four shapes:

| Call site | Shape | Lifetime | What it needs |
|---|---|---|---|
| `harness/run.py:304` | streaming | one turn — or **held** across turns | **bidirectional stdin**, stdout line limit, process-group kill |
| `harness/harness.py:201` | one-shot | single | stdin `DEVNULL` — `codex exec` must get EOF, not block |
| `bg_tasks.py:244` | shell | **outlives the turn** | `/bin/sh -c`, `start_new_session`, group SIGTERM |
| `app_backends.py:304` | service | **until idle** | **port + readiness + reverse proxy**, group signal, log tail |
| `app_backends.py:220` | one-shot | single | `install.sh`, same dir |
| `fork_helpers.py:73` | one-shot | single | `git`, timeout |
| `codex_login.py:138` | interactive | single | device code / URL returned to the user |

Three of those four shapes are the same primitive with switches off: one-shot
is *no stdin, wait for exit*; the background task is *detached*. Only the
Application backend needs something genuinely extra, and it is not the process
— it is the port (§6).

The table is one direction of the seam: the server starting work in the
runtime. There is a second, and it is easy to miss because today both ends are
the same machine — **work in the runtime calling the server back**. Every
in-turn tool call does it, and so does every Application backend that talks to
an agent. §7 is that direction.

The streaming row's lifetime needs its footnote. A CLI process is not always
one turn: `sessions/processes.py` keeps it **held** between turns — reused for
the next one, reaped when idle, capped by an LRU. A held process is an
`Execution` that nobody is currently reading, and it matters to `release()`
(§3): a tenant with a held process is not idle.

`tunnel.py:59` also spawns a process and is **not** on this list. It is
`cloudflared`, the site's own public endpoint. It belongs to the box Octopus
runs on, not to any tenant, and it stays a plain subprocess. The test for
"does this go through a Runtime" is *whose work is it* — a tenant's, or the
install's.

---

## 3. Granularity: one Runtime per tenant

A Runtime is **per account**, created on demand and released when the account
goes idle. Every one of that account's sessions, background tasks and
Application backends runs inside it.

The alternative — one per session — was rejected. It is the shape that makes a
cold start sit on the critical path of opening a chat, and it has no good
answer for an Application backend, which belongs to an *account* and outlives
every session that touched it.

What per-tenant costs is stated rather than smoothed over: **an account's own
sessions are not isolated from each other.** An agent that fills the disk or
exhausts memory takes down that account's other sessions *and its running
Applications*. The answer is cgroup limits and quotas inside the tenant's
runtime, not a second runtime per session — and it is strictly better than
today, where the same blast radius covers **every** account on the box
(`multi-tenancy.md` §2, row A).

**When a runtime starts and stops** is the server's decision, not the
provider's, and it is made from facts the server already has:

* `ensure(tenant)` is called before **every** `exec` for that tenant — an
  interactive turn, a scheduled fire, a delegation child, a background task,
  an Application backend, a `/rewind`'s `git`. It is idempotent, so calling it
  always is cheaper than knowing whether to. A scheduled fire for a tenant whose
  runtime is released is exactly the case it exists for.
* `release(tenant)` is called only when the tenant has **nothing left
  running**: no turn in flight, no held CLI process (§2), no detached
  background task, no Application backend. The existing reapers already decide
  each of those — the held-process idle reaper, the background task's exit, the
  Application backend's idle stop — so `release()` hangs off the last of them,
  after a grace period, rather than off a timer of its own.
* `release()` with something still running is a server bug. The contract does
  not ask a provider to guess whether to kill it.

`LocalRuntime.ensure()` and `release()` are no-ops. The box is already there.

---

## 4. The interface

As implemented in `server/runtime/base.py` (`PROTOCOL_VERSION = "runtime-1.0"`):

```python
class Runtime(Protocol):
    name: str

    # Lifecycle — per tenant, idempotent. Called by the server (§3).
    async def ensure(self, tenant: TenantId) -> None: ...
    async def release(self, tenant: TenantId) -> None: ...

    # Execution — one primitive, all seven call sites.
    async def exec(self, spec: ExecSpec) -> Execution: ...

    # Network, server → runtime — Applications, and only Applications (§6).
    async def expose(self, tenant: TenantId) -> Exposed: ...   # (port, dial)

    # Network, runtime → server — the base URL work in this tenant's runtime
    # uses to reach this server: MCP tools, the Application agent API (§7).
    # Synchronous: it is read while argv and env are assembled, and a provider
    # knows its own topology without asking anyone.
    def callback_base(self, tenant: TenantId) -> str: ...

    # Paths — the tenant's directories, as the runtime sees them (§8, §9).
    def paths(self, tenant: TenantId) -> UserPaths: ...
    def confine(self, requested: str | None, tenant: TenantId, *,
                extra_roots: list[str] | None = None) -> str: ...
```

```python
@dataclass(frozen=True)
class ExecSpec:
    tenant: TenantId               # an account id, or None for the install itself
    argv: Sequence[str]
    env: Mapping[str, str]         # the COMPLETE environment — see below
    cwd: str | None = None
    stdin: Literal["devnull", "pipe", "inherit"] = "devnull"
    stderr: Literal["pipe", "stdout"] = "pipe"   # "stdout" = one merged log stream
    process_group: bool = True     # own group: signalled as a unit, survives its caller
    line_limit: int | None = None  # stdout reader's max line, as today
    hints: Mapping[str, str] = {}  # provider-specific; LocalRuntime ignores them

class Execution(Protocol):
    stdin: AsyncWriter | None      # write / drain / close / is_closing
    stdout: AsyncReader | None     # readline / read / async for
    stderr: AsyncReader | None     # None when merged
    returncode: int | None
    async def wait(self) -> int: ...
    async def communicate(self) -> tuple[bytes, bytes]: ...
    def signal(self, sig: int, *, group: bool = True) -> bool: ...
```

Three shapes are the call sites', kept rather than tidied: `stdin="inherit"`
(the two Application scripts have always spawned that way; under systemd it is
`/dev/null`), `stderr="stdout"` (install.sh, start.sh and `codex login` pump one
ordered log), and `process_group=False` (`git`, a short child killed directly on
timeout). `signal` is synchronous and best-effort — it returns once the signal
is on its way, swallows the already-exited race, and is True only when a group
signal was sent — because every caller uses it from a teardown path that must
not itself block. `communicate` is on the protocol because "read both pipes to
EOF, then wait" is the one shape that cannot deadlock on a full pipe, and two
call sites need it.

The server never holds a bare provider: `get_runtime()` returns it wrapped in
`ManagedRuntime` (`registry.py`), which owns §3's decisions — `ensure` before
every `exec`, and `release` once every execution it handed out has been awaited
to the end and `runtime_release_grace_seconds` has passed with nothing new. The
provider is chosen by `runtime_provider`: `local`, or `package.module:Class`.

**`env` is the complete environment.** The provider adds nothing from the
server's own environment, because for any provider but `LocalRuntime` the
server's environment is the operator's: `OCTOPUS_AUTH_TOKEN`, the master-key
path, every secret the service was started with. Today three call sites build
the child's environment as `os.environ.copy()` plus overrides
(`harness/claude_code.py:328`, `harness/codex.py:205`, `harness/run.py:111`) —
on one box that is isolation level A, accepted by `multi-tenancy.md` §2; handed
to another tenant's VM it is the operator's keys shipped to a customer.
Inheritance is therefore the **caller's** explicit act, never the runtime's
implicit one. §10 keeps step 2 a pure refactor by having those callers pass
`os.environ` themselves, and makes the allowlist its own, later step.

The same rule bounds what a caller may put in `env`: the tenant's own
credential (its Claude token, its `CODEX_HOME`), its own MCP bearer, its own
paths. Nothing that belongs to the install or to another tenant.

Two deliberate omissions.

**No `pid`.** A pid is meaningless to a caller that is not in the same pid
namespace, and every current use of it is really "signal this process and its
children" — which is `signal(group=True)`. Exposing a pid would let a local
assumption back in through the one door the interface closes.

**No timeout.** `fork_helpers` wraps `communicate()` in `wait_for`; that is the
caller's policy and it composes fine over `wait()`. A timeout in the contract
would have to specify what it does to the process on expiry, and every provider
would answer differently.

---

## 5. Login is interactive, and that is a contract question

`codex_login.py` runs `codex login`, reads a device code or URL from its
output, and shows it to the user. It fits `exec` exactly — streaming stdout,
no stdin — so it needs nothing new.

What it does need is stated here so a second provider cannot get it wrong: the
credential the CLI writes lands **inside the runtime**, under
`paths(tenant).codex_home`. It is not copied back out, and the server never
holds it. A provider whose filesystem does not persist across `release()` will
log the user out every time the tenant goes idle, which is a provider bug, not
a login bug.

The same is true of the CLIs' **own state**, not only their credentials.
Claude Code keeps its configuration and its session transcripts under
`~/.claude` (`projects/<cwd-slug>/<session>.jsonl`, the files `--resume` reads);
Codex keeps its rollouts under `CODEX_HOME/sessions`. Under `LocalRuntime` that
is the host's `~/.claude`, shared by every account — the accepted level-A
exposure. Under a per-tenant runtime it is that tenant's home directory, which
is an improvement nobody has to build: it falls out of the CLI running there.
It also means the server can no longer reach those files with `Path` — see the
fork rows in §9.

---

## 6. Ports are where a naive interface breaks

`app_backends._free_port()` binds `127.0.0.1:0` **in the server process**,
reads the port number, closes the socket, and hands the number to the child.
Its own docstring is honest about the race:

> There is an unavoidable gap between closing this socket and the child binding
> it.

That trick works only because the server and the child share a network
namespace. The moment they do not, it allocates a port on the wrong machine.

So port allocation moves to the provider, and the proxy gets an address rather
than a number:

```python
@dataclass(frozen=True)
class Exposed:
    port: int        # what the child binds, inside the runtime
    dial: str        # what routers/applications.py connects to, from the server
```

For `LocalRuntime` these are the same loopback port and the behaviour — race
included — is unchanged. For anything else, `dial` is how the reverse proxy
reaches in, and the provider owns the mapping.

This is the one place where the interface is shaped by a provider that does not
exist yet. It is worth it: a port number is exactly the kind of local
assumption that compiles fine and fails in production.

---

## 7. The other direction: the runtime calls the server back

Ports are the server reaching *in*. The reverse is just as load-bearing and
just as invisible on one box: work inside the runtime reaching *out* to this
server, at an address the server handed it. Today that address is always
loopback, and there are exactly these places that hand it out:

| Call site | Who calls back | Address today |
|---|---|---|
| `harness/assembly.py` `_http_entries` | the CLI, for every MCP tool (Claude `--mcp-config`, Codex `-c mcp_servers.*`) | `http://127.0.0.1:{port}/mcp/<ns>/mcp` |
| `app_backends.py:108` | an Application backend, via `OCTOPUS_AGENT_API` | `http://127.0.0.1:{port}/apps/{id}/agent` |
| `harness/assembly.py` `build_callback_env` | the stdio sidecars — tests only since B1 | `OCTOPUS_API_BASE` |

Under any provider but `LocalRuntime`, `127.0.0.1` inside the runtime is the
runtime itself: every tool call in every turn fails, and every Application that
talks to an agent fails, the moment the first remote provider is switched on.

So the address comes from the provider: `callback_base(tenant)` returns the
base URL that work in *that tenant's* runtime uses to reach the server, and the
three sites above build from it instead of from `settings.port`.
`LocalRuntime` returns `http://127.0.0.1:{port}` — today's value, so the
refactor stays pure. A remote provider returns whatever its network gives it: a
private address, a per-tenant gateway, a tunnel.

What is **not** on the list matters as much. The in-process MCP tool bodies
call their own REST routes through `mcp_servers/_host.api_base()`. They run in
the server, not the runtime, so loopback is correct for them and stays.

Two requirements follow, and both belong to the server, not the provider:

* **The endpoint authenticates every call by bearer, never by where it came
  from.** Once the server is reachable from tenant runtimes, "the request came
  from inside" means "the request came from a tenant". Every callback already
  carries one — the MCP scope bearer, the Application's scoped token — and
  those are the only thing that may be trusted. `multi-tenancy-gaps.md` §7.3's
  expiry on MCP bearers stops being hardening here and becomes required: the
  bearer now sits in a process the tenant controls.
* **A provider may narrow reachability, but may not be relied on to.** A
  gateway that only lets tenant A's runtime reach tenant A's routes is welcome
  defence in depth. The bearer check is the boundary, so the conformance suite
  does not test it as one.

## 8. `confine()` becomes a provider responsibility

Today `confine()` is a pure function over host paths, and it is the security
boundary for `working_dir` (`multi-tenancy.md` §6). It cannot stay pure.

In a per-tenant runtime, confinement is *structural* — the runtime only has
that tenant's filesystem, so "inside my workspace" is not a check, it is a
property. `LocalRuntime.confine()` keeps today's logic verbatim, symlink
resolution and all. A VM-backed provider's answer is much simpler and much
stronger.

The hazard is the obvious one: **moving a security check behind an interface
means a provider can implement it wrong and nothing notices.** Two things
mitigate it, and both are requirements, not suggestions:

* `confine()` keeps its contract — *return an absolute path the tenant may use,
  or raise `WorkspaceError`* — and the 400-mapping handler stays where it is.
* The conformance suite (§10) runs the same confinement cases against every
  provider, including `..`, symlinks that escape, and a path that does not
  exist yet.

`users.extra_roots` is already marked in `multi-tenancy.md` §6 as a single-box
affordance the cloud version drops. It becomes exactly that: `LocalRuntime`
honours it, and a provider is free to ignore it.

---

## 9. Files are deliberately not in this version

`paths()` returns the tenant's directories, but there is no `read`/`write`/
`list` on the interface, and callers keep using `Path` directly.

That is a real limitation and it is chosen. Writing file primitives now means
designing them against one implementation where they are pure overhead —
`LocalRuntime.read()` would be `Path.read_bytes()` with extra steps — and
guessing at the granularity the second provider wants. The call sites that
touch files are known and small:

| Module | What it touches |
|---|---|
| `file_viewer.py` | reads one file from the session's cwd for `/showme` |
| `attachments.py` | writes uploads into `attachments/` |
| `agent_memory.py` | provisions `agents/<id>/memory/` |
| `large_prompts.py` | spills an oversize prompt, hands the model a path to `Read` |
| `applications.py` | the app directory tree |
| `harness/claude_code.py` `_fork_copy` | reads the parent's CLI transcript under `~/.claude/projects/<slug>/`, writes the fork's copy |
| `harness/codex.py` `_fork_copy` / `_find_rollout` | finds and copies a rollout under `CODEX_HOME/sessions/` |

Under `LocalRuntime` every one of these is correct as written. Under a remote
provider every one of them is broken, loudly and immediately — `Path` on the
server does not see the runtime's disk. That is the right failure: a second
provider cannot be half-finished without the first thing it touches failing.

The two fork rows are different in kind from the rest: they touch the **CLIs'
own state**, not Octopus's (§5). Under a per-tenant runtime those files are in
the tenant's home, so a full-copy fork is the first thing a remote provider
breaks after the callbacks. The natural shape is not a file primitive but a
step that *runs inside the runtime* — the copy is a few lines that can be an
`exec` like any other — and that is how it should move when it moves.

`large_prompts.py` is the sharpest illustration and worth keeping in view: it
writes a file *so the model can `Read` it*. Server-side and runtime-side are
the same disk today; the moment they are not, the spill must land where the
model can reach it, or the prompt silently points at nothing.

---

## 10. Order of work

1. **`server/runtime/` with `base.py` + `local.py` + `registry.py`.**
   `LocalRuntime` is a *pure refactor*: the current code moves, nothing
   changes. The test suite must pass unmodified — that is the acceptance
   criterion, not a nice-to-have.
2. **Route the seven call sites through `exec()`.** One at a time, each with
   its existing tests. `tunnel.py` is explicitly untouched. Each caller passes
   its environment **explicitly**: the three that use `os.environ.copy()` today
   pass `{**os.environ, **overrides}` themselves (§4), so the inheritance is
   still there — byte-for-byte — but now visible at the call site instead of
   hidden in the runtime.
3. **`expose()` for Application backends** (§6), replacing `_free_port()` at
   its one call site.
4. **`callback_base()` at the three sites that hand the server's address to
   the runtime** (§7). `LocalRuntime` returns today's loopback URL.
5. **`confine()` and `paths()` onto the interface** (§8), keeping the current
   implementation as `LocalRuntime`'s.
6. **`ensure()` / `release()` wired to the server's lifecycle** (§3):
   `ensure` before every `exec`, `release` from the last of the existing
   reapers. No-ops locally, so no behaviour change — but the wiring is what a
   second provider will exercise first, so it is tested now with a fake
   provider that records the calls.
7. **A provider conformance suite**, published for out-of-tree use. Same cases,
   any implementation:
   * exec shapes — streaming with stdin, one-shot with EOF, detached;
     process-group signalling; a detached execution surviving its caller;
   * a held `Execution` left unread and resumed (§2);
   * confinement — `..`, a symlink that escapes, a path that does not exist yet;
   * port round-trip: a child binds `expose().port`, the server dials
     `expose().dial`;
   * **callback round-trip**: a child in the runtime reaches
     `callback_base()` and gets the server's answer;
   * **environment isolation**: a variable set in the server process and not
     passed in `ExecSpec.env` is absent in the child;
   * state persistence: a file written under `paths(tenant)` survives
     `release()` then `ensure()` (§5 — the logged-out-on-idle bug).

   Since providers ship from other repositories on other schedules, this is
   the only thing that can tell one whether it still conforms — it is the
   contract, not a test folder.

Steps 1–6 are individually revertible and ship no behaviour change.

**Status (2026-10-11):** steps 1–7 are implemented on `runtime-provider` —
`server/runtime/` (`base`, `local`, `registry`, `conformance`), every spawn
site routed through `exec()` with its owning tenant, `expose()` and
`callback_base()` at their sites, `confine()` / `paths()` behind the interface,
and the lifecycle tested against a recording provider (`tests/test_runtime.py`).
The conformance suite is `server/runtime/conformance.py`, framework-free so an
out-of-tree provider runs it under its own runner; `LocalRuntime` passes it
both bare and wrapped. Steps 8 and 9 remain.

Two steps **do** change behaviour, and are listed separately because they are
fixes rather than refactors — each ships on its own, with its own tests, after
the refactor has landed:

8. **The environment allowlist.** Replace the explicit `os.environ` of step 2
   with an allowlist (`PATH`, `HOME`, `LANG`, `TERM` and the like) plus the
   call's own overrides — the shape `app_backends.py:102` already uses for
   Application backends. This is what makes §4's rule hold locally too: the
   operator's `OCTOPUS_AUTH_TOKEN` and master-key path stop reaching agents on
   the single box. It is `multi-tenancy-gaps.md` §7.6's hardening item, and it
   is required before any provider hosts another person.
9. **Fork as a runtime step.** Move the full-copy fork's transcript copy
   (§9) from server-side `Path` calls to an `exec` inside the tenant's runtime,
   so a remote provider can fork. `LocalRuntime` runs the same copy in a child
   process; the result on disk is identical.

Neither changes the protocol, so neither needs a version.

A note on the protocol's status once step 5 lands: it is published surface. The
in-tree freedom to rename a method because it reads better ends there, and a
breaking change needs a version, not a commit.

---

## 11. What this defers

* **Every provider except `LocalRuntime`.** Including the one this is for. And
  before the first one that hosts *other people* goes live, the server-side
  gaps in `multi-tenancy-gaps.md` (§1) must be closed — a VM per tenant does
  not stop one tenant acting on another's session through the server.
* **File primitives on the interface** (§9) — added when a second provider
  forces them, shaped by what it actually needs.
* **Isolation between an account's own sessions** (§3) — cgroups and quotas
  inside a tenant runtime, once there is a runtime that can enforce them.
* **Multi-host scheduling.** `ensure(tenant)` says nothing about *where*, and
  deliberately: a provider that spans machines owns placement, and an interface
  that tried to express it would be guessing.
* **Moving `tunnel.py`.** It is the install's, not a tenant's (§2).
* **Per-tenant databases.** `multi-tenancy.md` §11 already defers these to
  "when tenants move to their own containers". They become attractive at that
  point — a tenant with its own runtime and its own SQLite file is a tenant
  that can be moved by copying a directory — but nothing here depends on it.
* **SSO.** Unrelated to execution, and separable: the key hierarchy does not
  touch it. `users.data_key()` unwraps a DEK the master key wrapped, and
  `set_password()` only rewrites `password_hash` — so an identity provider
  replaces `authenticate()` and leaves §4 of `multi-tenancy.md` alone.
