"""Shared test fixtures."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point every test at its own throwaway store.db."""
    db_path = tmp_path / "vl" / "store.db"
    monkeypatch.setenv("VL_STORE_PATH", str(db_path))
    yield db_path


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point HOME at a throwaway directory for every test.

    Node roots (``~/orgs``), node logs (``~/tmp``) and the shared install
    (``~/.veritaslock``) are all found through ``Path.home()``, so no test can
    reach the real ones. A test that needs its own HOME still sets it over this.
    """
    home = tmp_path / "isolated-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    assert Path.home() == home
    return home
