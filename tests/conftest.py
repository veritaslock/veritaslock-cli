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


class LiveNetworkRefused(OSError):
    """A test tried to open a TCP connection: the live stack must never be reached."""


@pytest.fixture(autouse=True)
def no_live_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Refuse every IPv4/IPv6 connection from the test process.

    The seeded ``local`` environment points at ``localhost:8080``/``8082``, where the
    live stack may be running. HTTP in tests goes through respx, which never opens a
    socket, so a real connection can only be a test that forgot to stub a call. It
    fails here instead of reaching a real service. Unix sockets are left alone.
    Processes a test starts are not covered; those tests use closed ports or stand-ins.
    """
    import socket

    real_connect, real_connect_ex = socket.socket.connect, socket.socket.connect_ex

    def refuse(self: socket.socket, address: object) -> None:
        if self.family in (socket.AF_INET, socket.AF_INET6):
            raise LiveNetworkRefused(f"test tried to connect to {address!r}; stub the call with respx")
        real_connect(self, address)  # type: ignore[arg-type]

    def refuse_ex(self: socket.socket, address: object) -> int:
        if self.family in (socket.AF_INET, socket.AF_INET6):
            raise LiveNetworkRefused(f"test tried to connect to {address!r}; stub the call with respx")
        return real_connect_ex(self, address)  # type: ignore[arg-type]

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse_ex)
