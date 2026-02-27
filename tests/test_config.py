import os

import pytest


def test_settings_defaults():
    """Config loads with sensible defaults."""
    # Clear any env overrides for isolated test
    env_backup = {}
    for key in ("OCTOPUS_AUTH_TOKEN", "OCTOPUS_HOST", "OCTOPUS_PORT"):
        if key in os.environ:
            env_backup[key] = os.environ.pop(key)

    try:
        from server.config import Settings

        s = Settings(auth_token="test-token")
        assert s.auth_token == "test-token"
        assert s.host == "0.0.0.0"
        assert s.port == 8000
        assert s.default_working_dir == "."
        assert "http://localhost:5173" in s.cors_origins
    finally:
        os.environ.update(env_backup)


def test_settings_from_env(monkeypatch):
    """Config reads from environment variables."""
    monkeypatch.setenv("OCTOPUS_AUTH_TOKEN", "my-secret")
    monkeypatch.setenv("OCTOPUS_PORT", "9000")

    from server.config import Settings

    s = Settings()
    assert s.auth_token == "my-secret"
    assert s.port == 9000
