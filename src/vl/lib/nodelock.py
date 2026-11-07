"""The per-node lifecycle lock (vl-agent-cli-spec.md §5.1, vl-agent-spec.md §6).

An advisory ``flock`` on ``<node_root>/.lifecycle.lock``, outside ``data/`` (which
reset deletes), held for the whole of a start, stop or reset, so that `vl` and a
node agent on the same host never run two of them on one node at once:

- :func:`node_lock` waits up to ``wait`` seconds (10 by default), then raises
  :class:`NodeBusyError`, "another operation is in progress".
- :func:`try_node_lock` never waits: the repair that `vl node list` and `show`
  make uses it, and skips itself when the node is busy.

The descriptor is opened close-on-exec (and Python's descriptors are not
inheritable anyway), so a node spawned while the lock is held does not keep it:
the lock ends when the operation does, not when the node does.
"""

from __future__ import annotations

import fcntl
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

LOCK_FILE_NAME = ".lifecycle.lock"
DEFAULT_WAIT_SECONDS = 10.0
BUSY_DETAIL = "another operation is in progress"
_POLL_SECONDS = 0.05


class NodeBusyError(Exception):
    """Another process holds the node's lifecycle lock, and the wait ran out."""

    def __init__(self, node_root: Path) -> None:
        self.node_root = node_root
        super().__init__(BUSY_DETAIL)


def lock_path(node_root: Path) -> Path:
    """``<node_root>/.lifecycle.lock``: beside ``data/``, never inside it."""
    return node_root / LOCK_FILE_NAME


def _open(node_root: Path) -> int:
    return os.open(lock_path(node_root), os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)


def _try_flock(fd: int) -> bool:
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def _release(fd: int) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


@contextmanager
def node_lock(node_root: Path, *, wait: float = DEFAULT_WAIT_SECONDS) -> Iterator[None]:
    """Hold the node's lock for the block, waiting up to ``wait`` seconds for it.

    Raises :class:`NodeBusyError` when the wait runs out, and ``FileNotFoundError``
    when ``node_root`` doesn't exist (the directory is never created here).
    """
    fd = _open(node_root)
    deadline = time.monotonic() + wait
    try:
        while not _try_flock(fd):
            if time.monotonic() >= deadline:
                raise NodeBusyError(node_root)
            time.sleep(_POLL_SECONDS)
    except BaseException:
        os.close(fd)
        raise
    try:
        yield
    finally:
        _release(fd)


@contextmanager
def try_node_lock(node_root: Path) -> Iterator[bool]:
    """Hold the node's lock for the block if it is free; never wait.

    Yields ``True`` when held, ``False`` when another process has it (the caller
    skips its work). A node whose directory doesn't exist has no lock anyone
    could hold, so that yields ``True`` without one.
    """
    try:
        fd: int | None = _open(node_root)
    except FileNotFoundError:
        fd = None
    if fd is None:
        yield True
        return
    if not _try_flock(fd):
        os.close(fd)
        yield False
        return
    try:
        yield True
    finally:
        _release(fd)
