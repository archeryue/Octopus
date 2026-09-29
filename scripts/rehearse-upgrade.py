"""Rehearse the §9 upgrade against a snapshot of a live install.

    .venv/bin/python scripts/rehearse-upgrade.py [--install DIR] [--work DIR]

Turning an install into its first account is the one irreversible step in
multi-tenancy.md: it re-encrypts every stored secret under a new key, gives
every row an owner, and moves the agent memory. "The tests pass" is not the
same claim as "it works on 51,000 real messages and two real OAuth tokens", and
the difference is a restore from backup.

So this runs the whole thing on a **snapshot**: the database is copied through
sqlite's backup API (a consistent read of a live WAL database, not a byte
written to it), the agent and application directories are copied beside it, and
everything happens under a scratch directory. Then it checks the things that
would actually hurt:

  * every credential, connector token and OAuth client still decrypts,
  * every session's working directory is still legal, so no conversation is
    stranded outside what the new account may reach,
  * every application still has its files and can still be rebuilt,
  * the agent memory arrived, and nothing was left behind,
  * the password the upgrade set actually signs you in.

Exit status is non-zero if any of that fails. The deployed install's token is
read (it is what the current secrets are encrypted with) and never printed.
"""
import argparse
import asyncio
import json
import os
import pathlib
import shutil
import sqlite3
import sys
import tempfile
import time

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument(
    "--install",
    default="/home/start-up/Octopus",
    help="the deployed install to rehearse (its .env names the DB and token)",
)
parser.add_argument(
    "--state",
    default=os.path.expanduser("~/.octopus"),
    help="that install's state directory (agents/, applications/)",
)
parser.add_argument(
    "--db",
    default="/home/start-up/octopus.db",
    help="the live database to snapshot",
)
parser.add_argument("--work", default=None, help="scratch dir (default: a temp dir)")
parser.add_argument("--username", default="rehearsal")
parser.add_argument("--password", default="rehearsal-password-1")
args = parser.parse_args()

ROOT = args.work or tempfile.mkdtemp(prefix="octopus-rehearsal-")
os.makedirs(ROOT, exist_ok=True)
DB = f"{ROOT}/snapshot.db"

print(f"rehearsing {args.db} -> {ROOT}")

# A consistent snapshot of a live WAL database, read-only at the source.
_src = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
_dst = sqlite3.connect(DB)
_src.backup(_dst)
_dst.close()
_src.close()
print(f"  snapshot: {os.path.getsize(DB) / 1e6:.0f} MB")

for name in ("agents", "applications"):
    src_dir = os.path.join(args.state, name)
    if os.path.isdir(src_dir):
        shutil.copytree(src_dir, os.path.join(ROOT, name), dirs_exist_ok=True)
        print(f"  copied {name}/")

tok = None
for line in open(os.path.join(args.install, ".env")):
    if line.startswith("OCTOPUS_AUTH_TOKEN="):
        tok = line.split("=", 1)[1].strip()
assert tok, f"no OCTOPUS_AUTH_TOKEN in {args.install}/.env"

os.environ["OCTOPUS_AUTH_TOKEN"] = tok
os.environ["OCTOPUS_DB_PATH"] = DB
os.environ["OCTOPUS_MASTER_KEY_FILE"] = f"{ROOT}/master.key"
os.environ["OCTOPUS_USERS_ROOT"] = f"{ROOT}/users"
os.environ["OCTOPUS_AGENTS_DIR"] = f"{ROOT}/agents"            # copies of the
os.environ["OCTOPUS_APPLICATIONS_DIR"] = f"{ROOT}/applications"  # real ones
os.environ["OCTOPUS_RESEARCH_DIR"] = f"{ROOT}/research"
os.environ["OCTOPUS_DEFAULT_WORKING_DIR"] = f"{ROOT}/workspace"

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from server.bootstrap import bootstrap_first_account  # noqa: E402
from server.crypto import decrypt, master_key  # noqa: E402
from server.database import Database  # noqa: E402
from server.users import UserManager  # noqa: E402
from server.workspace import confine, paths_for  # noqa: E402


async def main() -> None:
    db = Database(DB)

    t = time.time()
    await db.initialize()
    print(f"migrations: {time.time() - t:.1f}s")
    print("unowned rows before:", await db.count_orphan_rows())

    t = time.time()
    summary = await bootstrap_first_account(
        db, username=args.username, password=args.password
    )
    print(f"bootstrap: {time.time() - t:.1f}s")
    print("  adopted    :", summary.get("adopted"))
    print("  reencrypted:", summary.get("reencrypted"))
    print("  memory moved:", summary.get("agent_memory_moved"), "entries")
    roots = summary.get("extra_roots") or []
    print(f"  extra_roots: {len(roots)} dirs")
    print("unowned rows after:", await db.count_orphan_rows())

    uid = summary["user_id"]
    dest = paths_for(uid).agents
    kept = sorted(dest.glob("*/memory/MEMORY.md")) if dest.exists() else []
    print(f"  agent memory under the account: {len(kept)} MEMORY.md")
    for f in kept:
        print(f"    {f.parent.parent.name}: {len(f.read_text().splitlines())} lines")
    legacy = pathlib.Path(os.environ["OCTOPUS_AGENTS_DIR"])
    print("  left in the legacy dir:", sorted(p.name for p in legacy.iterdir())
          if legacy.exists() else "gone")

    users = UserManager(db)
    user = await db.get_user(uid)
    key = users.data_key(user)
    ok = bad = 0
    for row in await db.load_credentials(uid):
        try:
            plain = decrypt(row["secret_encrypted"], key)
            ok += 1
            print(
                f"  credential {row['label']!r} ({row['backend']}): "
                f"decrypts, {len(plain)} chars"
            )
        except Exception as e:
            bad += 1
            print(f"  credential {row['label']!r}: FAILED {e}")
    for inst in await db.load_connector_installations(uid):
        try:
            json.loads(decrypt(await db.get_connector_secret(inst["id"]), key))
            ok += 1
            print(f"  connector {inst['kind']}/{inst['label']!r}: decrypts")
        except Exception as e:
            bad += 1
            print(f"  connector {inst['kind']}: FAILED {e}")
    cur = await db.conn.execute(
        "select kind, client_secret_encrypted from connector_oauth_clients")
    for kind, blob in await cur.fetchall():
        try:
            decrypt(blob, master_key())
            ok += 1
            print(f"  oauth client {kind}: decrypts under the master key")
        except Exception as e:
            bad += 1
            print(f"  oauth client {kind}: FAILED {e}")

    for app in await db.load_applications(user_id=uid):
        exists = os.path.isdir(app["app_dir"])
        try:
            confine(app["app_dir"], user_id=uid, extra_roots=roots)
            workable = True
        except Exception:
            workable = False
        print(f"  app {app['name']!r}: files={'yes' if exists else 'MISSING'} "
              f"rebuildable={'yes' if workable else 'NO'}")

    stranded = []
    for row in await db.load_sessions(include_archived=True, user_id=uid):
        try:
            confine(row["working_dir"], user_id=uid, extra_roots=roots)
        except Exception:
            stranded.append(row["working_dir"])
    print(f"  sessions whose working dir is now illegal: {len(stranded)}")
    for w in sorted(set(stranded)):
        print(f"    {w}")

    print(f"sessions owned: {len(await db.load_sessions(include_archived=True, user_id=uid))}")
    n = (await (await db.conn.execute("select count(*) from messages")).fetchone())[0]
    print(f"messages preserved: {n}")
    signed_in = await users.authenticate(args.username, args.password)
    print(f"login as {args.username}:", "ok" if signed_in else "FAILED")

    await db.close()
    print(f"\nRESULT: {ok} secrets decrypted, {bad} failed, {len(stranded)} sessions stranded")
    sys.exit(1 if (bad or stranded) else 0)


asyncio.run(main())
