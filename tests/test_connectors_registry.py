"""Connector registry + ConnectorBase shape (connectors.md §5.1-§5.3):
registration, the backend-neutral mcp_entry, system-prompt blurb, and the
tool-name-length invariant."""

from __future__ import annotations

import sys

import pytest

from server.connectors.base import (
    MAX_TOOL_NAME_LEN,
    ConnectorBase,
    ConnectorInstallation,
)
from server.connectors.registry import (
    KIND_REGISTRY,
    all_connectors,
    get_connector,
    register,
)


class _FakeProvider:
    kind = "dummy"
    authorize_url = "https://x/auth"
    token_url = "https://x/token"
    default_scopes = ["s"]
    pkce = True

    def build_authorize_url(self, **k):
        return "https://x/auth"

    async def exchange_code(self, **k):
        raise NotImplementedError

    async def refresh(self, refresh_token):
        raise NotImplementedError


class DummyConnector(ConnectorBase):
    kind = "dummy"
    display_name = "Dummy"
    category = "test"
    allows_multiple = True
    oauth = _FakeProvider()
    tools = ("search", "get", "create_draft", "send_draft")
    blurb_intro = "A dummy connector."


@pytest.fixture
def clean_registry():
    """Isolate global KIND_REGISTRY mutations from other tests."""
    saved = dict(KIND_REGISTRY)
    KIND_REGISTRY.clear()
    yield
    KIND_REGISTRY.clear()
    KIND_REGISTRY.update(saved)


def _install(install_id="abcdef123456") -> ConnectorInstallation:
    return ConnectorInstallation(
        id=install_id, kind="dummy", label="me@example.com", scopes=["s"]
    )


def test_register_get_all(clean_registry):
    c = DummyConnector()
    register(c)
    assert get_connector("dummy") is c
    assert get_connector("missing") is None
    assert c in all_connectors()


def test_mcp_key_and_tool_name():
    c = DummyConnector()
    inst = _install("abcdef999999")
    assert c.mcp_key(inst) == "dummy_abcdef"
    assert c.tool_name(inst, "search") == "mcp__dummy_abcdef__search"


def test_mcp_entry_shape():
    c = DummyConnector()
    inst = _install()
    callback_env = {
        "OCTOPUS_API_BASE": "http://127.0.0.1:8765",
        "OCTOPUS_AUTH_TOKEN": "tok",
        "OCTOPUS_SESSION_ID": "sess",
        "PYTHONPATH": "/repo",
    }
    entry = c.mcp_entry(inst, callback_env)
    assert entry["command"] == sys.executable
    assert entry["args"] == ["-m", "server.mcp_servers.connectors.dummy"]
    # Shared callback env is preserved and the installation id is injected.
    assert entry["env"]["OCTOPUS_API_BASE"] == "http://127.0.0.1:8765"
    assert entry["env"]["OCTOPUS_INSTALLATION_ID"] == inst.id
    # We didn't mutate the caller's env dict.
    assert "OCTOPUS_INSTALLATION_ID" not in callback_env


def test_system_prompt_blurb_lists_tools():
    c = DummyConnector()
    inst = _install()
    blurb = c.system_prompt_blurb(inst)
    assert "[dummy / me@example.com]" in blurb
    assert "A dummy connector." in blurb
    for verb in c.tools:
        assert c.tool_name(inst, verb) in blurb


def test_tool_name_length_invariant(clean_registry):
    """Every registered kind's tools must fit under the model's name limit,
    using a worst-case (12-char id → 6-char slice) installation."""
    register(DummyConnector())
    worst = _install("zzzzzz000000")
    for connector in all_connectors():
        for verb in connector.tools:
            name = connector.tool_name(worst, verb)
            assert len(name) <= MAX_TOOL_NAME_LEN, name


@pytest.mark.asyncio
async def test_default_health_check_ok_and_identity_not_implemented():
    c = DummyConnector()
    status = await c.health_check(_install(), "tok")
    assert status.ok is True
    with pytest.raises(NotImplementedError):
        await c.fetch_external_identity(object())  # type: ignore[arg-type]
