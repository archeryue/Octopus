"""Connector → backend wiring (connectors.md §5.6): an agent's enabled
connectors must surface as MCP server entries (per-installation key + token
env) and a system-prompt blurb in BOTH backends' build_args."""

from __future__ import annotations

import json

from server.connectors.base import ConnectorBase, ConnectorInstallation
from server.harness import RunConfig, get_harness


class _FakeProvider:
    kind = "dummy"
    pkce = True

    def build_authorize_url(self, **k):
        return "u"

    async def exchange_code(self, **k):
        raise NotImplementedError

    async def refresh(self, refresh_token):
        raise NotImplementedError


class DummyConnector(ConnectorBase):
    kind = "dummy"
    display_name = "Dummy"
    oauth = _FakeProvider()
    tools = ("search", "get")


def _pair():
    inst = ConnectorInstallation(id="abcdef123456", kind="dummy", label="me@x.com")
    return DummyConnector(), inst


def _arg_after(argv, flag):
    return argv[argv.index(flag) + 1]


# --- Claude backend --------------------------------------------------------


def test_claude_merges_connector_mcp_entry():
    conn, inst = _pair()
    run = get_harness("claude-code").create_run(
        RunConfig(session_id="s1", connectors=[(conn, inst)])
    )
    argv, _ = run.build_argv("hi", "/tmp", None)

    cfg = json.loads(_arg_after(argv, "--mcp-config"))["mcpServers"]
    key = conn.mcp_key(inst)  # dummy_abcdef
    assert key in cfg

    # The connector is served over HTTP by this app, not spawned
    # (polish-2026-09.md §4 B1). Note what does and does not move: the KEY stays
    # per-installation, because the tool name is built from it and
    # `mcp__dummy_abcdef__search` must not become something else — while the URL
    # is shared per connector *kind*, since one module serves every
    # installation. Key and URL are independent, which is what allows both.
    entry = cfg[key]
    assert entry["type"] == "http"
    assert entry["url"].endswith("/mcp/custom/mcp"), "user-defined kinds share a mount"
    assert "command" not in entry and "args" not in entry

    # Session AND installation now ride the credential, because one endpoint
    # serves every session and every account of a kind.
    from server.mcp_identity import verify

    scope = verify(entry["headers"]["Authorization"].removeprefix("Bearer "))
    assert scope is not None
    assert scope.session_id == "s1"
    assert scope.installation_id == inst.id, "the call must know WHICH account"

    # Built-ins still present.
    assert {"bg", "ask"} <= set(cfg)


def test_claude_appends_connector_blurb():
    conn, inst = _pair()
    run = get_harness("claude-code").create_run(
        RunConfig(session_id="s1", connectors=[(conn, inst)])
    )
    argv, _ = run.build_argv("hi", "/tmp", None)
    sp = _arg_after(argv, "--append-system-prompt")
    assert "== Connectors ==" in sp
    assert conn.tool_name(inst, "search") in sp


def test_claude_no_connectors_unchanged():
    run = get_harness("claude-code").create_run(RunConfig(session_id="s1"))
    argv, _ = run.build_argv("hi", "/tmp", None)
    cfg = json.loads(_arg_after(argv, "--mcp-config"))["mcpServers"]
    # Default built-in MCP set: bg + ask + ask_agent + research
    # (agent-collaboration.md §5.1; native-deep-research.md §7).
    assert set(cfg) == {"bg", "ask", "ask_agent", "research", "schedule"}
    assert "== Connectors ==" not in _arg_after(argv, "--append-system-prompt")


# --- Codex backend ---------------------------------------------------------


def test_codex_merges_connector_overrides_and_blurb():
    conn, inst = _pair()
    run = get_harness("codex").create_run(
        RunConfig(session_id="s1", connectors=[(conn, inst)])
    )
    argv, _ = run.build_argv("hi", "/tmp", None)
    joined = " ".join(argv)
    key = conn.mcp_key(inst)

    # Codex gets a URL plus the NAME of an env var to read the bearer from: it
    # has no way to take an arbitrary header (its --env is stdio-only), which is
    # why identity rides the credential rather than a header.
    assert f"mcp_servers.{key}.url=" in joined
    assert f"mcp_servers.{key}.bearer_token_env_var=" in joined
    assert f"mcp_servers.{key}.command=" not in joined, "nothing is spawned"
    # Developer-instructions blurb is injected via -c developer_instructions=…
    assert "== Connectors ==" in joined


def test_codex_no_connectors_has_no_connector_overrides():
    run = get_harness("codex").create_run(RunConfig(session_id="s1"))
    argv, _ = run.build_argv("hi", "/tmp", None)
    assert not any("mcp_servers.dummy_" in a for a in argv)
