"""The results file (vl-agent-spec.md §2.2, §4.4)."""

from __future__ import annotations

import csv
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from vl.agent.resultslog import HEADER, HEADER_LINE, ResultRow, ResultsLog, encode_row, format_time

T = datetime(2026, 10, 27, 9, 14, 6, 412_345, tzinfo=timezone.utc)


def row(detail: str = "pid 48213", **kw: object) -> ResultRow:
    base: dict[str, object] = dict(completed_at=T, correlation_id="7f3a1c2e", command="vl network start",
                                   org="globo", node=3, agent="dev-box", host="192.168.8.2",
                                   outcome="STARTED", exit_code=None, detail=detail)
    base.update(kw)
    return ResultRow(**base)  # type: ignore[arg-type]


def read_rows(path: Path) -> list[list[str]]:
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.reader(f, delimiter="|"))


def test_the_spec_example_line() -> None:
    assert HEADER_LINE == "time|id|command|org|node|agent|host|outcome|exit|detail\n"
    assert encode_row(row()) == "2026-10-27T09:14:06.412Z|7f3a1c2e|vl network start|globo|3|dev-box|" \
                                "192.168.8.2|STARTED||pid 48213\n"
    assert encode_row(row(outcome="FAILED", exit_code=69)).split("|")[7:9] == ["FAILED", "69"]


def test_time_is_utc_to_the_millisecond() -> None:
    plus2 = timezone(timedelta(hours=2))
    assert format_time(datetime(2026, 1, 2, 5, 6, 7, 9_999, tzinfo=plus2)) == "2026-01-02T03:06:07.009Z"


def test_file_and_directory_are_created_with_the_header(tmp_path: Path) -> None:
    path = tmp_path / "orgs" / "globo" / "agent" / "data" / "results.log"
    log = ResultsLog(path, max_bytes=10_000, keep=5)
    log.append(row())
    log.close()
    assert read_rows(path) == [list(HEADER), encode_row(row()).rstrip("\n").split("|")]


def test_the_header_is_not_written_again_on_restart(tmp_path: Path) -> None:
    path = tmp_path / "results.log"
    for _ in range(3):
        log = ResultsLog(path, max_bytes=10_000, keep=5)
        log.append(row())
        log.close()
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines.count(HEADER_LINE.rstrip("\n")) == 1
    assert len(lines) == 4


def test_each_row_is_flushed_at_once(tmp_path: Path) -> None:
    path = tmp_path / "results.log"
    log = ResultsLog(path, max_bytes=10_000, keep=5)
    log.append(row(detail="first"))
    assert path.read_text(encoding="utf-8").endswith("|first\n")  # before close
    log.close()


def test_comma_needs_no_quoting_and_pipe_or_quote_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "results.log"
    log = ResultsLog(path, max_bytes=10_000, keep=5)
    details = ["a, b, c", 'pipe | and "quotes"', "line one\nline two\r\nthree"]
    for d in details:
        log.append(row(detail=d))
    log.close()
    raw = path.read_text(encoding="utf-8").splitlines()
    assert raw[1].endswith("|a, b, c")
    assert len(raw) == 4  # one result is one line
    assert [r[9] for r in read_rows(path)[1:]] == ["a, b, c", 'pipe | and "quotes"', "line one line two three"]


def test_a_newline_in_any_field_becomes_a_space(tmp_path: Path) -> None:
    path = tmp_path / "results.log"
    log = ResultsLog(path, max_bytes=10_000, keep=5)
    log.append(row(command="vl org\nstop", agent="dev\rbox"))
    log.close()
    assert read_rows(path)[1][2:6] == ["vl org stop", "globo", "3", "dev box"]


def test_rotation_keeps_the_configured_number_of_files(tmp_path: Path) -> None:
    tmp_path = tmp_path / "data"
    path = tmp_path / "results.log"
    line = len(encode_row(row(detail="x" * 50)).encode())
    log = ResultsLog(path, max_bytes=len(HEADER_LINE) + 2 * line, keep=3)
    for i in range(10):
        log.append(row(detail=f"{i}".ljust(50, "x")))
    log.close()
    files = sorted(p.name for p in tmp_path.iterdir())
    assert files == ["results.log", "results.log.1", "results.log.2"]
    for name in files:
        rows = read_rows(tmp_path / name)
        assert rows[0] == list(HEADER)  # every file starts with the header
        assert 1 <= len(rows) - 1 <= 2
    newest = [r[9][0] for r in read_rows(path)[1:]]
    assert newest == ["8", "9"]
    assert [r[9][0] for r in read_rows(tmp_path / "results.log.1")[1:]] == ["6", "7"]


def test_utf8_whatever_the_locale(tmp_path: Path) -> None:
    path = tmp_path / "results.log"
    script = (
        "import sys; from pathlib import Path; sys.path[:0] = [sys.argv[2]]; "
        "from test_agent_resultslog import row; from vl.agent.resultslog import ResultsLog; "
        "log = ResultsLog(Path(sys.argv[1]), max_bytes=10000, keep=2); "
        "log.append(row(detail='\\u00d6l\\u00e7\\u00fc \\u2014 \\u6771\\u4eac')); log.close()"
    )
    env = {**os.environ, "LANG": "C", "LC_ALL": "C", "PYTHONIOENCODING": "ascii", "PYTHONUTF8": "0"}
    subprocess.run([sys.executable, "-c", script, str(path), str(Path(__file__).parent)], check=True, env=env)
    assert path.read_bytes().decode("utf-8").splitlines()[1].endswith("|Ölçü — 東京")
