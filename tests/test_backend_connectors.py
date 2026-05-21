"""Connector → backend wiring (connectors.md §5.6): an agent's enabled
connectors must surface as MCP server entries (per-installation key + token
env) and a system-prompt blurb in BOTH backends' build_args."""

from __future__ import annotations

import json

from server.backends.claude_code import ClaudeCodeBackend
from server.backends.codex import CodexBackend
from server.connectors.base import ConnectorBase, ConnectorInstallation


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
    be = ClaudeCodeBackend(session_id="s1", connectors=[(conn, inst)])
    argv, _ = be.build_args("hi", "/tmp", None)

    cfg = json.loads(_arg_after(argv, "--mcp-config"))["mcpServers"]
    key = conn.mcp_key(inst)  # dummy_abcdef
    assert key in cfg
    assert cfg[key]["args"] == ["-m", "server.mcp_servers.connectors.dummy"]
    # Token env: shared callback vars + the installation id.
    assert cfg[key]["env"]["OCTOPUS_INSTALLATION_ID"] == inst.id
    assert cfg[key]["env"]["OCTOPUS_API_BASE"].startswith("http://127.0.0.1:")
    assert cfg[key]["env"]["OCTOPUS_SESSION_ID"] == "s1"
    # Built-ins still present.
    assert {"viewer", "bg", "ask"} <= set(cfg)


def test_claude_appends_connector_blurb():
    conn, inst = _pair()
    be = ClaudeCodeBackend(session_id="s1", connectors=[(conn, inst)])
    argv, _ = be.build_args("hi", "/tmp", None)
    sp = _arg_after(argv, "--append-system-prompt")
    assert "== Connectors ==" in sp
    assert conn.tool_name(inst, "search") in sp


def test_claude_no_connectors_unchanged():
    be = ClaudeCodeBackend(session_id="s1")
    argv, _ = be.build_args("hi", "/tmp", None)
    cfg = json.loads(_arg_after(argv, "--mcp-config"))["mcpServers"]
    assert set(cfg) == {"viewer", "bg", "ask"}
    assert "== Connectors ==" not in _arg_after(argv, "--append-system-prompt")


# --- Codex backend ---------------------------------------------------------


def test_codex_merges_connector_overrides_and_blurb():
    conn, inst = _pair()
    be = CodexBackend(session_id="s1", connectors=[(conn, inst)])
    argv, _ = be.build_args("hi", "/tmp", None)
    joined = " ".join(argv)
    key = conn.mcp_key(inst)

    assert f"mcp_servers.{key}.command=" in joined
    assert any(
        a.startswith(f"mcp_servers.{key}.env.OCTOPUS_INSTALLATION_ID=") for a in argv
    )
    # Developer-instructions blurb is injected via -c developer_instructions=…
    assert "== Connectors ==" in joined


def test_codex_no_connectors_has_no_connector_overrides():
    be = CodexBackend(session_id="s1")
    argv, _ = be.build_args("hi", "/tmp", None)
    assert not any("mcp_servers.dummy_" in a for a in argv)
