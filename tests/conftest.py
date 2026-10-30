"""Every test runs against its own temporary config and data folders.

Without this, a test that forgets to set them reads and writes the real ones: it can leave
entries in your own config, or collide with a batch your dashboard is sending (the send lock
lives in the data folder). Tests that need specific folders still set their own.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _private_folders(tmp_path_factory, monkeypatch):
    base = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(base / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(base / "data"))
