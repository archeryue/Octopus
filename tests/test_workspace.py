"""Where an account may work (multi-tenancy.md §6).

The confinement check is one of the few places in this codebase where being
*almost* right is worthless: a check that compares strings, or that compares
before resolving symlinks, reads as protection and stops nobody. So the cases
here are the bypasses, not the happy path.
"""

from __future__ import annotations

import os

import pytest

from server.config import settings
from server.workspace import WorkspaceError, confine, paths_for


@pytest.fixture(autouse=True)
def _roots(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "users_root", str(tmp_path / "users"))
    monkeypatch.setattr(settings, "default_working_dir", str(tmp_path / "legacy"))
    (tmp_path / "legacy").mkdir()
    paths_for("u1").ensure()
    return tmp_path


class TestPaths:
    def test_each_account_gets_its_own_corner(self):
        a, b = paths_for("u1"), paths_for("u2")
        assert a.workspace != b.workspace
        assert not str(a.workspace).startswith(str(b.workspace))
        for name in ("agents", "applications", "attachments", "codex_home", "research"):
            assert getattr(a, name) != getattr(b, name)

    def test_before_accounts_the_legacy_layout_is_kept(self):
        """An install being upgraded must keep reading the agent memory and
        applications it already has; moving them is §9's job, done once and
        deliberately, not a surprise from path resolution."""
        assert str(paths_for(None).agents) == os.path.expanduser(settings.agents_dir)
        assert str(paths_for(None).research) == os.path.expanduser(
            settings.research_dir
        )


class TestConfinement:
    def test_the_default_is_the_workspace(self):
        assert confine(None, user_id="u1") == str(paths_for("u1").workspace.resolve())

    def test_inside_is_allowed_even_when_it_does_not_exist_yet(self):
        """An agent creating the directory it was pointed at is normal."""
        wanted = str(paths_for("u1").workspace / "a-project" / "src")
        assert confine(wanted, user_id="u1") == wanted

    def test_outside_is_refused(self):
        for outside in ("/etc", "/", os.path.expanduser("~")):
            with pytest.raises(WorkspaceError):
                confine(outside, user_id="u1")

    def test_dot_dot_cannot_climb_out(self):
        escape = str(paths_for("u1").workspace / ".." / ".." / "etc")
        with pytest.raises(WorkspaceError):
            confine(escape, user_id="u1")

    def test_a_symlink_cannot_walk_out(self, tmp_path):
        """The check that matters. Comparing the path as written would say yes
        to this, and the first thing through it is another account's files."""
        victim = tmp_path / "somewhere-else"
        victim.mkdir()
        link = paths_for("u1").workspace / "innocent"
        link.symlink_to(victim)

        with pytest.raises(WorkspaceError):
            confine(str(link), user_id="u1")
        with pytest.raises(WorkspaceError):
            confine(str(link / "deeper"), user_id="u1")

    def test_a_sibling_with_a_shared_prefix_is_not_inside(self, tmp_path):
        """`/…/users/u1-evil` starts with `/…/users/u1`. String prefixes say
        yes; this must not."""
        evil = tmp_path / "users" / "u1-evil" / "workspace"
        evil.mkdir(parents=True)
        with pytest.raises(WorkspaceError):
            confine(str(evil), user_id="u1")

    def test_another_accounts_workspace_is_refused(self):
        paths_for("u2").ensure()
        with pytest.raises(WorkspaceError):
            confine(str(paths_for("u2").workspace), user_id="u1")

    def test_the_accounts_own_application_directory_is_allowed(self):
        """The boundary is the account's root, not the workspace subdirectory
        inside it: an application's build session works in that application's
        directory, which lives beside the workspace rather than in it."""
        app_dir = paths_for("u1").applications / "notes"
        assert confine(str(app_dir), user_id="u1") == str(app_dir)

    def test_but_another_accounts_application_directory_is_not(self):
        paths_for("u2").ensure()
        with pytest.raises(WorkspaceError):
            confine(str(paths_for("u2").applications / "notes"), user_id="u1")

    def test_extra_roots_open_exactly_what_they_name(self, tmp_path):
        """The single-box affordance the cloud version drops: user #1 keeps
        working on a repository that lives outside any workspace."""
        repo = tmp_path / "Octopus"
        (repo / "server").mkdir(parents=True)
        assert confine(str(repo / "server"), user_id="u1", extra_roots=[str(repo)])
        # Naming one path does not open its parent.
        with pytest.raises(WorkspaceError):
            confine(str(tmp_path), user_id="u1", extra_roots=[str(repo)])

    def test_before_accounts_nothing_is_confined(self):
        """Because there is no owner to confine to, and the install's existing
        sessions point wherever they already point."""
        assert confine("/etc", user_id=None) == "/etc"

    def test_the_error_says_where_work_is_allowed(self):
        with pytest.raises(WorkspaceError) as e:
            confine("/etc", user_id="u1")
        assert str(paths_for("u1").workspace) in e.value.message
