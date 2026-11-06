# Devices

> **Status:** in-progress — Design: machines the user owns — a laptop, a rented GPU box, a home server, a phone — join Octopus as Devices that its agents can use, through `octo device …` and a small outbound-connecting daemon, `octod`.

> An agent today can act only where its session runs. This plan lets a user
> connect the machines they own and grant agents the use of them. It is built
> **after `octo`** ([`octo-cli.md`](octo-cli.md)), because device control is a
> part of that standard CLI and stands on its interface layer, identity and
> scopes. Nothing is built yet.

---

## 1. What this is

A user has more than one machine:

- the box Octopus runs on;
- a laptop holding the repository they work in;
- a GPU machine rented on Lambda;
- a server at home;
- a phone.

They want their agents to use those machines: run the training job on the GPU
box, read a log on the laptop, start a notebook server and open it from the
phone. Octopus should know the user's devices, which agent may use which, and
what each was used for.

### Runtime and Device are different things

- A **Runtime** is where a session *lives*: the harness CLI runs there and keeps
  its transcript there. It is the agent's brain
  ([`runtime-providers.md`](https://github.com/archeryue/Octopus/blob/runtime-provider/docs/plans/runtime-providers.md),
  on the `runtime-provider` branch).
- A **Device** is a machine an agent *reaches out to* from its runtime, to run a
  command, move a file, or forward a port. It is the agent's hands.

Most of the time an agent needs hands on another machine, not to move its whole
session there. This is DeepSeek's "decoupling the brain from the hands" and
"bring your own machine"
([`agent-router-and-channels.md`](agent-router-and-channels.md) §2.1).

**Two different assumptions, so two different designs:**

| | Runtime | Device |
|---|---|---|
| Assumption | **Always online.** Its going down is an outage the platform recovers from, never a state the design plans for | **May go offline at any time.** Being unreachable is normal |
| Holds | All of a session's state: history, workspace, CLI state | Nothing of a session's. It runs what it is asked and returns the result |
| Managed by | The Runtime platform, uniformly. A local machine that is to be a Runtime is virtualized and managed like any other node, not treated as special | Pairing, grants, approvals; offline is reported and work can queue |

**A Device is never a Runtime.** One machine that is both offline-able and the
persistent home of sessions is the hybrid this plan refuses. Its cost —
offline policies for sessions, schedules that cannot fire, history that cannot
be read — is not worth what it buys. A machine that can be always on and should
host sessions joins the Runtime platform. A machine that comes and goes is a
Device.

---

## 2. Two kinds of device

| Kind | Examples | What an agent can do there |
|---|---|---|
| **Shell device** | Linux, macOS, Windows / WSL2, cloud VMs, a rented GPU box | Run commands, move files, forward ports, run long jobs |
| **Capability device** | A phone | Use what the platform exposes: notifications, location, photos, calendar, approvals. **No shell** — iOS gives none, and a phone is the wrong place for one |

A phone is also where the user *controls* Octopus (the PWA, [`mobile.md`](mobile.md)).
As a device it is a capability provider and the place approvals are asked for
(§5).

---

## 3. Design

```
 laptop / GPU box / home server                    Octopus site
 ┌───────────────────────────┐   outbound,     ┌──────────────────────────────┐
 │ octod                     │   one long-lived│ device registry · grants     │
 │  exec · files · ports     │ ───────────────▶│ approvals · audit (Ledger)   │
 │  jobs · capabilities      │◀── commands ────│ /api/v1/devices  (octo layer) │
 │  the device's own creds   │   on the same   └──────────────┬───────────────┘
 │  never leave it           │   connection                   │
 └───────────────────────────┘                  an agent, in its own runtime:
                                                octo device exec gpu1 -- nvidia-smi
```

### 3.1 `octod`, the daemon

- **It connects out; nothing connects in.** It holds one long-lived connection
  to the site (WebSocket or gRPC over TLS), and commands come back over it. A
  laptop behind home NAT, a cloud box behind its firewall, and a WSL2 distro
  behind the Hyper-V firewall all work without opening a port — which is what
  a machine that comes and goes needs.
- **Pairing.**
  - `octod pair` prints a one-time code.
  - The user confirms it in Octopus (or `octo device approve <code>`).
  - The daemon gets a device key, scoped to that user's account.
  - Revoking the key on the site cuts the device off at once.
- **It reports what it is:** OS and architecture, CPU / GPU / memory / disk, the
  directories it exposes, whether it allows command execution, and its
  capabilities (§2).
- **Its own small protocol**, not the Runtime protocol. It needs one-shot and
  long-running commands, files, forwards and capabilities. It does not need what
  only a session's home needs: held interactive processes, bidirectional stdin,
  a caller-supplied complete environment, harness hosting.
  - It shares the low-level process, signal and file primitives with Runtime
    implementations, as a library, not as a protocol.
  - The environment rule is the opposite of a Runtime's. A command runs in the
    **device's own environment** with the device's own credentials; the
    caller's environment does not come in.
- `octod` is a natural Rust program: a single static binary on every kind of
  machine.

### 3.2 Device control is part of `octo`

It is the standard CLI ([`octo-cli.md`](octo-cli.md)), not a separate tool. It
uses the same interface layer (`/api/v1/devices`), the same bearers, the same
scopes, JSON output and progressive help.

```
octo devices                                   # list: online?, kind, capabilities
octo device show <dev>
octo device exec <dev> -- <command…>           # run, stream output, exit code
octo device cp <dev>:<path> <path>             # and the other direction
octo device forward <dev>:<port> [--public]    # e.g. a notebook server, opened from the phone
octo device notify <phone> <text>              # capability devices
octo bg run --device <dev> <command>           # a long job; its result returns as a new turn
```

The last line is what makes a GPU box useful. A training run takes hours. The
agent starts it with the existing bg machinery
([`turn-safety.md`](turn-safety.md)), ends its turn, and the result is delivered
as a new turn when the job finishes.

The pairing and management verbs (`pair`, `approve`, `grant`, `revoke`) belong
to the same CLI, for people.

### 3.3 Grants: which agent may use which device

- **No agent can use any device by default.** The user grants a device to an
  Agent, with a scope.

  | Scope | Allows |
  |---|---|
  | `device:read` | List directories, read files |
  | `device:exec` | Run commands |
  | `device:write` | Change files |
  | `device:forward` | Expose a port |

  Options narrow it further: allowed directories, a command allowlist, a
  time window.
- The device sets its own ceiling: a device that exposes only `~/data` read-only
  can never be granted more, whatever the site says.
- A grant is on the Agent, so every session of that Agent may use it. A
  hand-made session gets nothing extra until its Agent is granted.

### 3.4 Coding on a device's repository

Claude Code's and Codex's built-in read, search and edit tools act on the
filesystem of the machine the CLI runs on. An agent in its Runtime reaching a
laptop through `octo device exec` would edit through `cat` and `sed`, which is
worse and costs more tokens.

The answer is the ordinary workflow, not running the harness on the device:

1. The code is in git. The agent works in its own checkout in its Runtime, with
   its native tools, and pushes a branch.
2. The device pulls and runs or tests it where the hardware or data is.

What this does not cover is uncommitted local work on the device (§8).

### 3.5 Offline

- A device that is offline is reported as such — `octo` exits with a distinct
  code and the reason. Nothing hangs.
- A job may be **queued** for the device's return (`--when-online`), shown in the
  Ledger as waiting. DeepSeek does the same for intermittently online machines.
- Wake-on-LAN, for a home machine that supports it, is a device option rather
  than a promise.

### 3.6 Cloud machines that cost money by the hour

A rented GPU box is only worth keeping on while it works. A later **device
provider** adapter (Lambda first) can:

- start the machine when a job targets it;
- install `octod` from a bootstrap script;
- stop the machine after it has been idle for a set time.

The first version needs none of this: install `octod` on a box that is
already rented.

### 3.7 Phones

A companion surface (iOS Shortcuts first, an app if needed) registers the phone
as a capability device. It carries:

- **Notifications** — "the training run finished", with a link.
- **Approvals** (§5) — the yes/no for a sensitive action.
- **Capabilities the user enables** — location, photos, calendar — each its own
  grant.

---

## 4. The Ledger records every device action

Each command, file transfer, forward and job records:

- which session and Agent ran it;
- the device and the command;
- start and end times, the exit code;
- an output excerpt, with the full output spilled.

It sits on the item of work it was part of
([`agent-router-and-channels.md`](agent-router-and-channels.md) §5.3). "What did
agents do on my laptop this week" becomes one query.

---

## 5. Security

Connecting agents to every machine a user owns turns one compromise into all of
them. The DeepSeek article lists this as its largest open problem (its §8.4). The
first version therefore ships with all of these:

1. **Deny by default; grant per Agent, per device, per scope** (§3.3). The device
   enforces its own ceiling, so a compromised site cannot exceed it.
2. **Approval for sensitive actions.** These need a person's yes, asked on the
   phone, with what will run and where:
   - the first use of a grant;
   - writes outside a scratch directory;
   - deletion;
   - anything not on an allowlist.
3. **Credentials never leave the device.** An agent uses the device's own `gh`,
   cloud CLI or SSH keys *on the device*. They are never read into a model's
   context or sent to the site. This is the "use a credential without touching
   it" requirement from the same article.
4. **Untrusted output is data.** What a device returns is treated as data with
   provenance, never as instructions, like Channel content
   ([`agent-router-and-channels.md`](agent-router-and-channels.md) §5.4).
5. **A kill switch on the device.** `octod pause` and `octod unpair` work
   locally, without the site.
6. **Audit and revocation.** Every action is in the Ledger (§4); device keys and
   grants are revocable on the site and take effect at once.
7. **Tenancy.** A device belongs to one account and is never visible to another
   ([`multi-tenancy.md`](multi-tenancy.md)).

---

## 6. Order of work

This plan starts **after `octo` step 1** ([`octo-cli.md`](octo-cli.md) §3), whose
interface layer, bearers and scopes it extends.

1. **Shell devices.** `octod` (pairing, outbound connection, `exec`, files,
   forward), the device registry and grants on the interface layer, the
   `octo device …` verbs, Ledger records.
   *Done when* an agent on the Octopus box runs a command on a paired laptop
   under a grant, and the Ledger shows it.
2. **Jobs and approvals.** `octo bg run --device`, `--when-online` queueing, the
   phone approval flow for sensitive actions.
3. **Phones as capability devices** (§3.7).
4. **Cloud device providers** (§3.6), Lambda first.

---

## 7. Open questions

1. `octod` in Rust from the start, or Python first like `octo`? It runs on every
   kind of machine, which argues for a static binary from day one.
2. The connection: WebSocket or gRPC, and how a long `exec` stream is
   multiplexed with control messages.
3. File transfer for large data (datasets, checkpoints): through the site, or
   device-to-device when both are reachable?
4. Whether a forward may ever be public, or only reachable through Octopus with
   the user's session.
5. Approval UX when the phone is not at hand: a timeout that denies, or a
   fallback channel?

## 8. What this defers

- **Running sessions on a Device.** A Device is the agent's hands, never its
  home (§1).
  - The one need this leaves uncovered is heavy editing of local files that
    cannot go through git.
  - If that need proves real, the answers are mounting the device's directory
    into the Runtime, or making that machine an always-on Runtime node.
  - It is never a session home that may go offline.
- Fleet management for teams (shared devices across an organization's
  accounts). That is Kraken's, with its own authorization model.
- Desktop GUI control (screen, mouse, keyboard). Shell, files and capabilities
  first.
