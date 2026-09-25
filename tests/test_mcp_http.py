"""The MCP namespaces, served in-process instead of spawned.

Each namespace used to be a subprocess per session: 7 per session at ~39 MB
PSS, ~85% of Octopus's own memory, ~247 ms of import cost each on the
session-start path (polish-2026-09.md §4 B1). Served over streamable-HTTP from
this app, nothing is spawned.

Two properties are load-bearing and easy to break, so they are pinned here:
the tool NAME must not move (both CLIs build `mcp__<key>__<tool>` from the
config key, so a key change silently invalidates every prompt that documents
them), and one session's bearer must not resolve as another's.
"""

from __future__ import annotations

import pytest

from server import mcp_http
from server.harness import assembly
from server.mcp_identity import McpScope, mint, verify


class TestIdentity:
    def test_a_bearer_round_trips_to_its_scope(self):
        assert verify(mint("sess-a")) == McpScope("sess-a", None)

    def test_an_installation_rides_along(self):
        assert verify(mint("sess-a", "inst-1")) == McpScope("sess-a", "inst-1")

    def test_one_sessions_bearer_is_not_anothers(self):
        a, b = mint("sess-a"), mint("sess-b")
        assert a != b
        assert verify(a).session_id == "sess-a"
        assert verify(b).session_id == "sess-b"

    def test_a_tampered_bearer_proves_nothing(self):
        good = mint("sess-a")
        flipped = good[:-1] + ("0" if good[-1] != "0" else "1")
        assert verify(flipped) is None

    def test_a_relabelled_bearer_proves_nothing(self):
        """Swapping the session in the clear part must not survive: the
        signature covers it, so the claim no longer verifies."""
        good = mint("sess-a")
        _, install, sig = good.split(".")
        assert verify(f"sess-b.{install}.{sig}") is None

    @pytest.mark.parametrize("bad", [None, "", "nope", "a.b", "a.b.c.d"])
    def test_malformed_bearers_are_refused(self, bad):
        assert verify(bad) is None


class TestMountedNamespaces:
    def test_every_namespace_is_mounted(self):
        from server.main import _mcp_mounts

        for name in ("bg", "ask", "ask_agent", "research", "schedule",
                     "github", "gmail", "custom"):
            assert mcp_http.mount_path(name) in _mcp_mounts

    def test_connectors_share_a_mount_per_kind(self):
        """One module serves every installation of a kind, so the URL is
        per-kind while the config key stays per-installation."""
        assert mcp_http.mount_path("gmail") == "/mcp/gmail"


class TestAssemblyEmitsHttpEntries:
    def test_a_session_gets_http_entries(self):
        entries = assembly.select_mcp_servers(None, [], {}, session_id="s-1")
        assert entries, "expected the builtin namespaces"
        assert all(e.is_http for e in entries)
        assert all(e.url.endswith("/mcp") for e in entries)
        assert all(not e.command for e in entries), "nothing should be spawned"

    def test_the_key_is_unchanged_so_tool_names_do_not_move(self):
        """`mcp__bg__run` must stay `mcp__bg__run`. The key is what both CLIs
        build the tool name from."""
        entries = assembly.select_mcp_servers(["bg", "ask"], [], {}, session_id="s-1")
        assert {e.key for e in entries} == {"bg", "ask"}

    def test_each_entry_carries_its_own_session_scope(self):
        entries = assembly.select_mcp_servers(["bg"], [], {}, session_id="s-9")
        assert verify(entries[0].credential) == McpScope("s-9", None)

    def test_two_sessions_get_distinguishable_entries(self):
        a = assembly.select_mcp_servers(["bg"], [], {}, session_id="s-a")[0]
        b = assembly.select_mcp_servers(["bg"], [], {}, session_id="s-b")[0]
        assert a.url == b.url, "same mount"
        assert a.credential != b.credential, "different scope"

    def test_an_agents_selection_is_respected(self):
        entries = assembly.select_mcp_servers(["bg"], [], {}, session_id="s-1")
        assert [e.key for e in entries] == ["bg"]

    def test_without_a_session_it_falls_back_to_stdio(self):
        """`build_argv` inspects argv without a session; there is nothing to
        scope a bearer to, so those entries stay spawnable."""
        entries = assembly.select_mcp_servers(["bg"], [], {"X": "1"})
        assert entries and not entries[0].is_http
        assert entries[0].command


class TestRendering:
    def test_claude_renders_http_with_the_bearer_inline(self):
        from server.harness import claude_code

        e = assembly.select_mcp_servers(["bg"], [], {}, session_id="s-1")[0]
        rendered = claude_code._mcp_entry(e)
        assert rendered["type"] == "http"
        assert rendered["url"] == e.url
        assert rendered["headers"]["Authorization"] == f"Bearer {e.credential}"
        assert "command" not in rendered

    def test_codex_renders_a_url_and_an_env_var_name(self):
        """Codex cannot take an arbitrary header — `--env` is stdio-only there —
        so it gets the URL plus the NAME of a var to read the bearer from."""
        from server.harness import codex

        e = assembly.select_mcp_servers(["bg"], [], {}, session_id="s-1")[0]
        ctx = type("Ctx", (), {"mcp_servers": [e]})()
        args = codex._mcp_config_args(ctx)
        flat = " ".join(args)
        assert "mcp_servers.bg.url=" in flat
        assert "mcp_servers.bg.bearer_token_env_var=" in flat
        assert "OCTOPUS_MCP_BEARER_BG" in flat
        assert "mcp_servers.bg.command=" not in flat, "nothing is spawned"

    def test_codex_puts_the_bearer_in_the_turn_env(self):
        from server.harness import codex

        e = assembly.select_mcp_servers(["bg"], [], {}, session_id="s-1")[0]
        ctx = type("Ctx", (), {"mcp_servers": [e]})()
        env: dict[str, str] = {}
        codex._apply_mcp_bearers(env, ctx)
        assert env["OCTOPUS_MCP_BEARER_BG"] == e.credential
