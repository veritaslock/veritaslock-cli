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
