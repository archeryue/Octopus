"""Where a user's files live, and where a session is allowed to work.

Single-user Octopus put everything under one set of directories and let a
session name any absolute path on the box as its working directory. Neither
survives accounts (multi-tenancy.md §6):

* every directory becomes a function of a user, so one account's agent memory,
  applications and uploads cannot be another's;
* a working directory has to resolve *inside* that user's workspace, or the
  first thing an agent does with a shell is read someone else's.

Both eras are the same shape. `paths_for(None)` — the pre-accounts install —
answers the directories Octopus has always used, and confinement is off,
because there is no owner to confine to and the one operator's existing
sessions point wherever they already point. From the first account on, every
answer is per-user and confinement is on.

**Symlinks are resolved before the check, not after.** A check against the
literal path is decoration: `ln -s /home/someone-else ~/work/theirs` walks
straight through it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import settings


class WorkspaceError(Exception):
    """A path outside what this account may reach."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class UserPaths:
    """One account's corner of the disk."""

    workspace: Path
    agents: Path
    applications: Path
    attachments: Path
    large_prompts: Path
    codex_home: Path
    research: Path

    def ensure(self) -> UserPaths:
        for path in (
            self.workspace,
            self.agents,
            self.applications,
            self.attachments,
            self.large_prompts,
            self.codex_home,
            self.research,
        ):
            path.mkdir(parents=True, exist_ok=True)
        return self


def _expand(raw: str) -> Path:
    return Path(raw).expanduser()


def user_root(user_id: str) -> Path:
    return _expand(settings.users_root) / user_id


def paths_for(user_id: str | None) -> UserPaths:
    """Where this account's files live — or the install's, before accounts.

    The pre-accounts answer is deliberately the legacy layout rather than a
    `users/None/` directory: an install being upgraded must keep reading the
    agent memory and applications it already has, and moving them is §9's job,
    done once and deliberately, not something path resolution does by surprise.
    """
    if user_id is None:
        return UserPaths(
            workspace=_expand(settings.default_working_dir),
            agents=_expand(settings.agents_dir),
            applications=_expand(settings.applications_dir),
            attachments=_expand(settings.attachments_dir),
            large_prompts=_expand(settings.large_prompts_dir),
            codex_home=_expand(settings.codex_home_dir),
            research=_expand(settings.research_dir),
        )
    root = user_root(user_id)
    return UserPaths(
        workspace=root / "workspace",
        agents=root / "agents",
        applications=root / "applications",
        attachments=root / "attachments",
        large_prompts=root / "large-prompts",
        codex_home=root / "codex",
        research=root / "research",
    )


def normalise_root(raw: str) -> Path:
    """Vet one `extra_roots` entry, or raise `WorkspaceError`.

    An extra root is a hole in the confinement, so the rules are about what the
    hole may be rather than about typos:

    * **Absolute**, because a relative path means something different depending
      on where the server was started.
    * **Real, and a directory** — a path that does not exist yet would be
      allowed to become anything later, including a symlink somewhere else.
    * **Resolved**, so what is stored is what `confine()` will compare against;
      storing the unresolved spelling would let a symlink change the meaning of
      an approved root after the fact.
    * **Not the filesystem root, and not a home directory's parent**: `/` or
      `/home` do not open a repository, they switch confinement off while
      leaving it looking switched on.
    """
    text = (raw or "").strip()
    if not text:
        raise WorkspaceError("An extra root cannot be empty")
    path = Path(text).expanduser()
    if not path.is_absolute():
        raise WorkspaceError(f"{text} is not an absolute path")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise WorkspaceError(f"{text} does not exist") from exc
    if not resolved.is_dir():
        raise WorkspaceError(f"{text} is not a directory")
    if resolved == Path(resolved.root) or resolved in (Path("/home"), Path("/Users")):
        raise WorkspaceError(
            f"{resolved} is too broad to be an extra root — name the directory "
            "you actually work in"
        )
    return resolved


def _within(candidate: Path, root: Path) -> bool:
    """Is `candidate` inside `root`, both already resolved?

    `Path.is_relative_to` rather than string prefixes, which say yes to
    `/home/archer-evil` for a root of `/home/archer`.
    """
    return candidate == root or candidate.is_relative_to(root)


def allowed_roots(user_id: str | None, extra_roots: list[str] | None = None) -> list[Path]:
    """Everywhere this account may put a working directory.

    The account's **root**, not just its workspace. The workspace is where a
    person works and stays the default, but the account owns more than that —
    an application's build session legitimately works inside that application's
    directory, which lives beside the workspace rather than in it.

    This does not weaken anything across accounts, which is the boundary that
    matters: one account's root contains only that account's files. It does
    mean an agent can read its own account's stored credentials, which it could
    anyway — the CLI it is already running holds them.
    """
    if user_id is None:
        return []
    roots = [user_root(user_id).resolve()]
    for raw in extra_roots or []:
        try:
            roots.append(_expand(raw).resolve())
        except OSError:  # pragma: no cover - an unreadable configured path
            continue
    return roots


def confine(
    requested: str | None,
    *,
    user_id: str | None,
    extra_roots: list[str] | None = None,
) -> str:
    """The absolute working directory a session may use, or raise.

    Resolved with symlinks followed *before* the comparison, because that is
    the only version of this check that means anything. A path that does not
    exist yet still resolves — `Path.resolve()` does not require existence —
    so creating a session in a directory the agent will make is still allowed,
    while `..` and symlinks are already collapsed by the time we compare.
    """
    paths = paths_for(user_id)
    if not requested:
        return str(paths.workspace.resolve())

    expanded = _expand(requested)
    if user_id is None:
        # Pre-accounts: unchanged behaviour, because there is no owner to
        # confine to and the install's existing sessions point where they do.
        return str(expanded.resolve())

    # A *relative* path from an account holder means "inside my workspace" —
    # the folder name they typed in the new-session form — not a path relative
    # to wherever the server process happens to have been launched. Resolving it
    # against the server cwd (the old behaviour) sent a bare folder name to
    # `/home/start-up/<name>`, outside the workspace, and the session was
    # refused. An absolute path (or a `~`-path) is still taken literally and
    # checked against the allowed roots below.
    if expanded.is_absolute():
        candidate = expanded.resolve()
    else:
        candidate = (paths.workspace / expanded).resolve()

    roots = allowed_roots(user_id, extra_roots)
    if any(_within(candidate, root) for root in roots):
        return str(candidate)
    raise WorkspaceError(
        f"{requested} is outside your workspace. Sessions work inside "
        f"{paths.workspace}."
    )

