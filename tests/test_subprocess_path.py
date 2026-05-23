"""Regression tests for PATH augmentation when spawning node-based CLIs.

`claude`/`codex` are `#!/usr/bin/env node` scripts; a service PATH that omits
the nvm bin makes the shebang's `node` lookup fail (exit 127). `augmented_path`
prepends the per-user install dirs so the child resolves node."""

import os

from server.harness.run import _fallback_path_dirs, augmented_path


def test_augmented_path_prepends_extra_then_fallbacks_then_base():
    p = augmented_path(base="/usr/bin:/bin", extra_dir="/opt/foo/bin")
    parts = p.split(os.pathsep)
    assert parts[0] == "/opt/foo/bin"  # resolved CLI's own dir wins
    assert p.endswith("/usr/bin:/bin")  # base preserved at the end
    # the per-user fallback dirs (e.g. ~/.local/bin) are present
    assert any(d.endswith("/.local/bin") for d in parts)


def test_augmented_path_defaults_base_to_environ(monkeypatch):
    monkeypatch.setenv("PATH", "/sentinel/bin")
    assert augmented_path().endswith("/sentinel/bin")


def test_fallback_dirs_include_nvm_glob_shape():
    # The nvm bin (where node + npm-global codex live) is the dir the service
    # PATH misses; the fallback list reaches for it explicitly.
    dirs = _fallback_path_dirs()
    assert any(".local/bin" in d for d in dirs)
