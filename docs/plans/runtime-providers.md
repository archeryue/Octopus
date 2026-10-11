# Runtime providers (`runtime-1.0`)

> **Status:** design. The execution layer becomes an interface with two
> implementations — the local one Octopus has always had, and whatever the
> cloud version brings.

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
implementation that is known but absent — and §9 holds the first version to a
pure refactor precisely so the fiction stays cheap if the second one changes
shape.

**Providers live outside this repository.** `LocalRuntime` is the only one that
ships in-tree, and that is the intended end state rather than a starting
condition: a provider is a separate project on its own release cadence, written
by someone who cannot read this codebase's private assumptions. Two consequences
run through everything below — the protocol is **versioned and stable**, not an
internal detail refactored at will, and the conformance suite (§9) is the
contract rather than the documentation. An interface whose only other
implementation lives next door would not need either, which is exactly why
designing for that case produces a worse interface.

**It must never make the single-box install worse.** Octopus runs on your own
laptop against your own subscription; that is the product. `LocalRuntime` is a
first-class implementation and the default, not a degraded fallback. Any
contract clause that is awkward for local execution is a bug in the contract.

---

## 2. The seam, as it actually exists

Seven places spawn a process. They are four shapes:

| Call site | Shape | Lifetime | What it needs |
|---|---|---|---|
| `harness/run.py:304` | streaming | one turn | **bidirectional stdin**, stdout line limit, process-group kill |
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

`LocalRuntime.ensure()` is a no-op. The box is already there.

---

## 4. The interface

```python
class Runtime(Protocol):
    # Lifecycle — per tenant, idempotent.
    async def ensure(self, tenant: TenantId) -> None: ...
    async def release(self, tenant: TenantId) -> None: ...

    # Execution — one primitive, all seven call sites.
    async def exec(self, spec: ExecSpec) -> Execution: ...

    # Network — Applications, and only Applications (§6).
    async def expose(self, tenant: TenantId) -> Exposed: ...

    # Paths — the tenant's directories, as the runtime sees them (§7, §8).
    def paths(self, tenant: TenantId) -> UserPaths: ...
    def confine(self, requested: str | None, tenant: TenantId) -> str: ...
```

```python
@dataclass(frozen=True)
class ExecSpec:
    tenant: TenantId
    argv: Sequence[str]
    cwd: str
    env: Mapping[str, str]
    stdin: Literal["none", "pipe"] = "none"
    detach: bool = False          # survives the caller; own process group
    line_limit: int | None = None # stdout reader's max line, as today

class Execution(Protocol):
    stdin: AsyncWriter | None
    stdout: AsyncIterator[bytes]
    stderr: AsyncIterator[bytes]
    async def wait(self) -> int: ...
    async def signal(self, sig: int, *, group: bool = True) -> None: ...
```

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

## 7. `confine()` becomes a provider responsibility

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
* The conformance suite (§9) runs the same confinement cases against every
  provider, including `..`, symlinks that escape, and a path that does not
  exist yet.

`users.extra_roots` is already marked in `multi-tenancy.md` §6 as a single-box
affordance the cloud version drops. It becomes exactly that: `LocalRuntime`
honours it, and a provider is free to ignore it.

---

## 8. Files are deliberately not in this version

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

Under `LocalRuntime` every one of these is correct as written. Under a remote
provider every one of them is broken, loudly and immediately — `Path` on the
server does not see the runtime's disk. That is the right failure: a second
provider cannot be half-finished without the first thing it touches failing.

`large_prompts.py` is the sharpest illustration and worth keeping in view: it
writes a file *so the model can `Read` it*. Server-side and runtime-side are
the same disk today; the moment they are not, the spill must land where the
model can reach it, or the prompt silently points at nothing.

---

## 9. Order of work

1. **`server/runtime/` with `base.py` + `local.py` + `registry.py`.**
   `LocalRuntime` is a *pure refactor*: the current code moves, nothing
   changes. The test suite must pass unmodified — that is the acceptance
   criterion, not a nice-to-have.
2. **Route the seven call sites through `exec()`.** One at a time, each with
   its existing tests. `tunnel.py` is explicitly untouched.
3. **`expose()` for Application backends** (§6), replacing `_free_port()` at
   its one call site.
4. **`confine()` and `paths()` onto the interface** (§7), keeping the current
   implementation as `LocalRuntime`'s.
5. **A provider conformance suite**, published for out-of-tree use. Same cases,
   any implementation: exec shapes, process-group signalling, detached
   survival, confinement, port round-trip. Since providers ship from other
   repositories on other schedules, this is the only thing that can tell one
   whether it still conforms — it is the contract, not a test folder.

Steps 1–4 are individually revertible and ship no behaviour change.

A note on the protocol's status once step 5 lands: it is published surface. The
in-tree freedom to rename a method because it reads better ends there, and a
breaking change needs a version, not a commit.

---

## 10. What this defers

* **Every provider except `LocalRuntime`.** Including the one this is for.
* **File primitives on the interface** (§8) — added when a second provider
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
