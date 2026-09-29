"""The master key that wraps every user's data key (multi-tenancy.md §4).

It is created rather than demanded, which puts three failure modes inside this
file's remit: the file must be created privately, an operator-supplied key must
win, and an *empty* key file must stop the server rather than be replaced —
because replacing it silently orphans every stored credential on the box.
"""

from __future__ import annotations

import os
import stat

import pytest

from server import crypto
from server.config import settings


@pytest.fixture(autouse=True)
def _isolated_master_key(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "master_key", "")
    monkeypatch.setattr(settings, "master_key_file", str(tmp_path / "master.key"))
    crypto._MASTER_CACHE.clear()
    yield
    crypto._MASTER_CACHE.clear()


def test_it_is_created_on_first_use_and_is_stable(tmp_path):
    path = tmp_path / "master.key"
    assert not path.exists()

    first = crypto.master_key()
    assert path.exists()
    assert len(first) > 20

    crypto._MASTER_CACHE.clear()
    assert crypto.master_key() == first, "a second boot invented a new key"


def test_it_is_created_private(tmp_path):
    """0600 from the open(), not a chmod afterwards — between the two there is
    a window where the key is world-readable."""
    crypto.master_key()
    mode = stat.S_IMODE(os.stat(tmp_path / "master.key").st_mode)
    assert mode == 0o600, f"master key is mode {mode:o}"


def test_an_operator_supplied_key_wins(tmp_path):
    """The hook a secret manager hangs on; the cloud version uses nothing else."""
    settings.master_key = "handed-in-from-somewhere-else"
    assert crypto.master_key() == "handed-in-from-somewhere-else"
    assert not (tmp_path / "master.key").exists(), "wrote a file it should not need"


def test_an_empty_key_file_refuses_rather_than_regenerating(tmp_path):
    """The dangerous case. A truncated file must not be read as "no key yet":
    generating a replacement turns every stored secret into noise, quietly."""
    (tmp_path / "master.key").write_text("   \n")
    with pytest.raises(ValueError) as e:
        crypto.master_key()
    assert "OCTOPUS_MASTER_KEY" in str(e.value)


def test_wrapping_round_trips_and_is_bound_to_the_master_key():
    dek = crypto.new_dek()
    wrapped = crypto.wrap_dek(dek)
    assert wrapped != dek
    assert crypto.unwrap_dek(wrapped) == dek

    with pytest.raises(ValueError):
        crypto.unwrap_dek(wrapped, key="a-different-master-key")
