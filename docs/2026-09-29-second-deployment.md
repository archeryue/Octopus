# A second Octopus on the same box

> How `bak.endlex.ai` was stood up to run the `user-1.0` branch beside the
> stable install, and what had to be true for that not to disturb it.

The point of a second deployment is to find what the tests cannot. It did, on
the first request: a connector token answered 500 because neither
`_CREDENTIAL_COLS` nor `_CONNECTOR_COLS` selected `user_id`, so every secret
was being decrypted with the wrong key — loudly for connectors, silently for
credentials. No amount of hermetic testing would have found it, because every
test that checked a secret derived the key itself.

## The shape

| | stable | next |
|---|---|---|
| host | `octo.endlex.ai` | `bak.endlex.ai` |
| port | 8080 | 8090 |
| code | `main`, `/home/start-up/Octopus` | `user-1.0`, `/home/start-up/octopus-user-1.0` |
| state | `~/.octopus`, `~/octopus.db` | `~/.octopus-dev` |
| service | `octopus.service` | `deploy/octopus-next.service` |

One cloudflared tunnel serves both — a third ingress rule, nothing else. Its
`config.yml` is backed up before every edit.

## What keeps the two apart

**Separate everything.** Port, database, state directory, master key, users
root. The second install's environment lives in `~/.octopus-dev/serve.env` so
the worktree's own `.env` stays what development and the test suite use; real
environment variables win over `.env`, which is what makes that work.

**The copied rows are repointed before the server ever starts.** A snapshot of
the live database still says its sessions work in `/home/start-up/Octopus` and
its applications live under `~/.octopus/applications`. Left alone, an agent on
the test install would write into the directories the running install works in.
Every `working_dir` and `app_dir` is rewritten under `~/.octopus-dev` first —
paths inside the live state directory to their copies, everything else to an
empty `sandbox/<name>`.

**The copied credentials are neutralised once they have been proven.** This is
the part that is easy to get wrong: a snapshot is read-only at the source, but
the OAuth *refresh tokens* inside it are live. If the second install refreshes
one successfully, the provider may rotate it — and the running install's stored
copy stops working. A read-only copy can still reach out and break something.
So: prove the read path once (the GitHub token decrypted and returned), then
clear the secrets and mark the installations `needs_reconnect`. The same goes
for the Codex credential, which is a *directory* — the second install points
`OCTOPUS_CODEX_HOME_DIR` at an empty one rather than at the copy of the live
`auth.json`. To exercise a connector or the Codex backend on the test install,
sign that install in separately.

**Schedules are disabled in the copy.** They own real model turns, they would
fire unattended against empty sandbox directories, and they cost money.

## Standing it up

```bash
# 1. Snapshot the live data (sqlite's backup API; nothing is written to the
#    source) and repoint every path — see scripts/rehearse-upgrade.py for the
#    same operation against a scratch dir.
# 2. Build the frontend: the backend serves web/dist, not web/src.
cd web && bun run build
# 3. The service.
sudo cp deploy/octopus-next.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now octopus-next
# 4. The hostname.
cloudflared tunnel route dns octopus bak.endlex.ai
#    then add the ingress rule to ~/.cloudflared/config.yml and restart:
sudo systemctl restart cloudflared
```

**`SIGHUP` is not a reload.** cloudflared 2026.9.3 treats it as a shutdown;
systemd's `Restart=always` brings it back with the new config about eight
seconds later. Either way the tunnel drops briefly and every hostname on it
blips — `octo.endlex.ai` included. There is no zero-downtime edit to a shared
tunnel's ingress; a second tunnel would have avoided it, at the cost of making
the eventual hostname swap a two-file change.

## Promoting it later is a data cutover, not a DNS change

The second install holds a snapshot taken at a moment. Pointing
`octo.endlex.ai` at it later would show data frozen at that moment and leave
everything since in the other database. The switch is:

1. stop `octopus.service`, so nothing more is written;
2. take a **fresh** snapshot and repoint it;
3. run the upgrade on that copy (`POST /api/auth/bootstrap`);
4. swap the two ingress rules and restart cloudflared.

It stays reversible for as long as the old database is untouched, which is
exactly why the cutover copies rather than migrates in place.
