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
