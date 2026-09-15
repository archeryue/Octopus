# Backing up and moving a deployment

A self-hosted Octopus keeps its state in **several places, not one**, and the
two most important ones are outside the directory you would guess. Backing up
`~/.octopus/` alone loses your session history and every agent you defined.

This is the method, not a script — the paths below are the defaults, and each
is overridable (`Settings` uses the `OCTOPUS_` env prefix, so `applications_dir`
is `OCTOPUS_APPLICATIONS_DIR`, and so on). Check `server/config.py` for the
current list before trusting this one.

## 1. Where the state actually lives

**The database — in the service's working directory, not `~/.octopus/`.**
`db_path` defaults to the *relative* path `octopus.db`, so it lands wherever
the process was started from (for a systemd unit, its `WorkingDirectory`). It
holds sessions, messages, agents, schedules, credentials, connector
installations, applications and bridge mappings — everything the UI shows. Find
it with:

```bash
systemctl show octopus -p WorkingDirectory --value    # then: ls $THAT/octopus.db
```

SQLite runs in WAL mode, so `octopus.db-wal` and `octopus.db-shm` sit beside it
and are part of the database (§3).

**Engine transcripts — under the CLI's own config dir.** Claude Code stores
each session's real conversation at
`~/.claude/projects/<cwd-slug>/<session-uuid>.jsonl`, and `--resume` reads
*that*, not our database. Octopus keeps its own copy of the messages for the UI;
the engine's copy is what lets an agent continue a conversation.

Losing either one loses a different half:

| lost | consequence |
|---|---|
| `octopus.db` | sessions, agents, schedules, credentials all gone |
| `~/.claude/projects/` | sessions still listed, but agents can't resume — every turn starts cold |

The second failure is survivable but visible: the harness detects the dangling
resume id, clears it, and replays recent history from our own record
(`server/fork_helpers.py`, `wrap_for_lost_history`). Recall is shallower for
that turn.

**`~/.octopus/` — the rest**, one subdirectory per concern:

| dir | what it is | needed in a restore? |
|---|---|---|
| `agents/` | per-agent memory dirs the agent writes to | **yes** — it's the agent's long-term memory |
| `applications/` | agent-built web apps, plain static files | **yes** — nothing else has a copy (§5) |
| `attachments/` | files you uploaded, one subdir per session | yes, if you want old messages' files to resolve |
| `codex/` | per-credential `CODEX_HOME` (Codex auth + state) | yes, or re-run the Codex device login after |
| `large-prompts/` | spill files for prompts too big for argv | no — transient |
| `research/` | deep-research artefacts | only if you want old reports |
| `fork/` | full working-directory copies made by `/fork` | usually **no** — see §5 |

**Credentials, outside both**: `~/.claude/.credentials.json` (Claude login),
`~/.codex/auth.json` (host-level Codex login), and the `.env` holding
`OCTOPUS_AUTH_TOKEN` and any tunnel config.

## 2. The minimum backup set

```
<WorkingDirectory>/octopus.db          # plus -wal and -shm, or checkpoint first
~/.octopus/                            # excluding fork/ (§5)
~/.claude/projects/                    # engine transcripts
~/.claude/.credentials.json            # Claude login
~/.codex/                              # Codex login (if you use Codex)
<your .env>                            # auth token, tunnel settings
```

## 3. Copy the database safely

Do **not** copy `octopus.db` from under a running server: WAL mode means recent
writes live in `-wal`, and a copy taken mid-write can be torn. Either stop the
service first, or take a consistent snapshot with SQLite's backup API (there is
no `sqlite3` CLI dependency in this project, so use the venv's Python):

```bash
.venv/bin/python - <<'PY'
import sqlite3
src = sqlite3.connect("octopus.db")          # the live file
dst = sqlite3.connect("octopus-backup.db")   # a consistent snapshot
with dst:
    src.backup(dst)
src.close(); dst.close()
PY
```

That produces one self-contained file with no `-wal`/`-shm` to carry along.

## 4. The migration gotcha: transcripts are keyed by path

`~/.claude/projects/<cwd-slug>/` — the slug is derived from the session's
**absolute working directory**, with separators flattened. The database also
stores each session's `working_dir` as an absolute path.

So a deployment only moves cleanly to a machine where **those absolute paths are
identical**. Restore under a different username or a different project location
and you get sessions that list fine and then can't resume, because the engine
looks for a slug directory that doesn't exist.

If the paths must change, you have to migrate both sides together: rename the
slug directories under `~/.claude/projects/` to match the new paths, and update
`working_dir` on the session rows. Verify on a copy before trusting it — a
mismatch is silent until an agent tries to answer.

## 5. Two directories that behave unlike the rest

**`fork/` grows without bound.** Every `/fork` (the full-copy kind) duplicates
an entire working directory under `~/.octopus/fork/`, and nothing prunes it.
It is usually the largest thing in `~/.octopus/` by a wide margin, and it is
**regenerable** — the forks' sessions still exist in the database. Excluding it
from a backup is normally right; deleting old entries is safe once you no longer
need those working copies.

**`applications/` has no other copy.** Agent-built apps are plain files owned by
Octopus, deliberately outside the git repo (so app code never lands in version
control). That also means **no history and no rollback** — if an agent breaks an
app there is nothing to revert to. Back it up, and consider `git init` inside an
app directory you care about. This is a known deferral, recorded in
`plans/applications.md` §10.

## 6. Restore, in order

1. **Stop the service** on both ends.
2. Restore `octopus.db` into the new deployment's working directory (or set
   `OCTOPUS_DB_PATH` to point at it).
3. Restore `~/.octopus/` (minus `fork/`), `~/.claude/projects/`, and the
   credential files.
4. Check the absolute paths match (§4) before starting anything.
5. Start the service and confirm `GET /health` answers.
6. **Verify with a real turn**, not just a page load: open a session that has
   history and send a message that requires prior context ("what were we just
   doing?"). If it answers from the conversation, the transcripts landed
   correctly; if it starts cold, §4 is what went wrong.
7. Check the Harness page for credentials needing re-authorization — an OAuth
   token can be expired independently of whether the file copied.
