"""``$HOME/orgs/<org>/agent/data/results.log`` (vl-agent-spec.md §2.2, §4.4).

Pipe-delimited text, UTF-8, one result per line, and nothing else: a header at
the top of a new or just-rotated file (not again when the agent restarts), then
one row per result, each written and flushed at once. Quoting is RFC 4180's with
``|`` as the delimiter (Python's ``csv`` reads it back); a newline in any field
becomes a space, so one result is always one line. The file rotates at
``max_bytes`` to ``results.log.1``, ``.2``, …, keeping ``keep`` files in all.
`vl agent results` and `vl`'s wait read it, so the format is an interface.
"""

from __future__ import annotations

import csv
import io
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO

HEADER = ("time", "id", "command", "org", "node", "agent", "host", "outcome", "exit", "detail")


@dataclass(frozen=True)
class ResultRow:
    completed_at: datetime
    correlation_id: str
    command: str
    org: str
    node: int
    agent: str
    host: str
    outcome: str
    exit_code: int | None
    detail: str


def format_time(when: datetime) -> str:
    """UTC, ISO 8601 to the millisecond: ``2026-10-27T09:14:06.412Z``."""
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    utc = when.astimezone(timezone.utc)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}Z"


def _one_line(value: str) -> str:
    return value.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")


def _encode(fields: tuple[str, ...]) -> str:
    buf = io.StringIO()
    csv.writer(buf, delimiter="|", quoting=csv.QUOTE_MINIMAL, lineterminator="\n").writerow(
        [_one_line(f) for f in fields]
    )
    return buf.getvalue()


def encode_row(row: ResultRow) -> str:
    return _encode((
        format_time(row.completed_at),
        row.correlation_id,
        row.command,
        row.org,
        str(row.node),
        row.agent,
        row.host,
        row.outcome,
        "" if row.exit_code is None else str(row.exit_code),
        row.detail,
    ))


HEADER_LINE = _encode(HEADER)


class ResultsLog:
    """Appends rows to the results file, rotating it at ``max_bytes``. Thread-safe."""

    def __init__(self, path: Path, *, max_bytes: int, keep: int) -> None:
        self.path = path
        self._max_bytes = max_bytes
        self._keep = max(1, keep)
        self._lock = threading.Lock()
        self._file: TextIO | None = None

    def _open(self) -> TextIO:
        if self._file is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            f = open(self.path, "a", encoding="utf-8", newline="")
            if os.fstat(f.fileno()).st_size == 0:
                f.write(HEADER_LINE)
                f.flush()
            self._file = f
        return self._file

    def _rotate(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None
        oldest = self._keep - 1
        if oldest == 0:
            self.path.unlink(missing_ok=True)
            return
        Path(f"{self.path}.{oldest}").unlink(missing_ok=True)
        for i in range(oldest - 1, 0, -1):
            src = Path(f"{self.path}.{i}")
            if src.exists():
                os.replace(src, f"{self.path}.{i + 1}")
        os.replace(self.path, f"{self.path}.1")

    def append(self, row: ResultRow) -> None:
        line = encode_row(row)
        with self._lock:
            f = self._open()
            size = os.fstat(f.fileno()).st_size  # every write is flushed
            if size > len(HEADER_LINE) and size + len(line.encode("utf-8")) > self._max_bytes:
                self._rotate()
                f = self._open()
            f.write(line)
            f.flush()

    def close(self) -> None:
        with self._lock:
            if self._file is not None:
                self._file.close()
                self._file = None


# --------------------------------------------------------------------------- #
# Reading (`vl agent results`, and later `vl`'s wait): one result per line, so
# counting and ordering are by line, and a header is never a result.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Line:
    """One result as the file has it: the raw line (no newline) and its fields."""

    raw: str
    fields: tuple[str, ...]

    def get(self, column: str) -> str:
        i = HEADER.index(column)
        return self.fields[i] if i < len(self.fields) else ""


def parse_line(raw: str) -> Line | None:
    """A result line; ``None`` for a header, a blank line or one that does not parse."""
    text = raw.rstrip("\r\n")
    if not text:
        return None
    try:
        fields = tuple(next(csv.reader([text], delimiter="|")))
    except (csv.Error, StopIteration):
        return None
    if fields == HEADER:
        return None
    return Line(text, fields)


def result_files(path: Path) -> list[Path]:
    """The current file and its rotated ones that exist, newest first:
    ``results.log``, ``results.log.1``, ``results.log.2``, …"""
    rotated = []
    for p in path.parent.glob(f"{path.name}.*"):
        suffix = p.name[len(path.name) + 1:]
        if suffix.isdigit() and p.is_file():
            rotated.append((int(suffix), p))
    current = [path] if path.is_file() else []
    return current + [p for _, p in sorted(rotated)]


def _lines(text: str) -> list[Line]:
    return [line for line in (parse_line(raw) for raw in text.splitlines()) if line is not None]


def read_file(path: Path) -> list[Line]:
    """A file's results, oldest first; nothing if it is gone (rotated meanwhile)."""
    try:
        return _lines(path.read_text(encoding="utf-8", errors="replace"))
    except FileNotFoundError:
        return []


def newest_first(path: Path, limit: int | None = None, *, current: list[Line] | None = None) -> list[Line]:
    """Up to ``limit`` results (all with ``None``), newest first, from the current file
    and then the rotated ones, so that a rotation never leaves a short answer.
    ``current`` is the current file's results when the caller has already read them."""
    found: list[Line] = []
    rotated = [p for p in result_files(path) if p != path]
    found.extend(reversed(current if current is not None else read_file(path)))
    for p in rotated:
        if limit is not None and len(found) >= limit:
            break
        found.extend(reversed(read_file(p)))
    return found if limit is None else found[:limit]


class Follower:
    """``tail -F`` of the current results file: the lines added after the point where it
    was opened, across a rotation (the old file is read to its end first, then the new
    one from its start), and across the file not existing yet. A partly written line is
    held back until its newline arrives."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._f: TextIO | None = None
        self._ino: int | None = None
        self._partial = ""

    def open_at_end(self) -> list[Line]:
        """Open the current file and read it all: its results now, oldest first. Following
        starts after them, so nothing is missed or shown twice."""
        if not self._open():
            return []
        assert self._f is not None
        return _lines(self._read_complete())

    def open_from(self, ino: int | None, size: int) -> list[Line]:
        """Follow from where the file was when ``(ino, size)`` was recorded (`vl`'s wait
        records it before it sends a command, so a result written at once is not missed).

        Returns the results already written since then, oldest first. If the file was
        rotated in between, the rest of the old one (now ``.1``) comes first; if it did
        not exist then, everything in it now is new.
        """
        found: list[Line] = []
        old = Path(f"{self.path}.1")
        if ino is not None:
            try:
                current_ino: int | None = os.stat(self.path).st_ino
            except FileNotFoundError:
                current_ino = None
            if current_ino != ino and old.is_file() and os.stat(old).st_ino == ino:
                with open(old, "r", encoding="utf-8", errors="replace", newline="") as f:
                    f.seek(size)
                    found += _lines(f.read())
                size = 0
            elif current_ino != ino:
                size = 0
        if not self._open():
            return found
        assert self._f is not None
        if size and os.fstat(self._f.fileno()).st_size >= size:
            self._f.seek(size)
        return found + _lines(self._read_complete())

    def _open(self) -> bool:
        try:
            f = open(self.path, "r", encoding="utf-8", errors="replace", newline="")
        except FileNotFoundError:
            return False
        self._f = f
        self._ino = os.fstat(f.fileno()).st_ino
        self._partial = ""
        return True

    def _read_complete(self) -> str:
        assert self._f is not None
        text = self._partial + self._f.read()
        cut = text.rfind("\n") + 1
        self._partial = text[cut:]
        return text[:cut]

    def _rotated(self) -> bool:
        try:
            st = os.stat(self.path)
        except FileNotFoundError:
            return False  # rotated away and not created yet: keep reading the old one
        assert self._f is not None
        return st.st_ino != self._ino or st.st_size < self._f.tell()

    def poll(self) -> list[Line]:
        """The results added since the last call."""
        if self._f is None:
            if not self._open():
                return []
        found = _lines(self._read_complete())
        if self._rotated():
            assert self._f is not None
            rest, self._partial = self._partial + self._f.read(), ""  # the old file is complete now
            found += _lines(rest)
            self.close()
            if self._open():
                found += _lines(self._read_complete())
        return found

    def close(self) -> None:
        if self._f is not None:
            self._f.close()
            self._f = None
