"""`vl agent results` (vl-agent-cli-spec.md §6): the agent's own data/results.log and
its rotated files, written here with the agent's real writer (ResultsLog)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from _helpers import add_service_account
from vl.agent import resultslog
from vl.agent.resultslog import HEADER_LINE, ResultRow, ResultsLog
from vl.app import app
from vl.commands import agent as agent_cmd
from vl.lib import store

runner = CliRunner()
T0 = datetime(2026, 11, 14, 9, 0, tzinfo=timezone.utc)
HEADER = HEADER_LINE.rstrip("\n")
WIDE = {"COLUMNS": "200"}


@pytest.fixture(autouse=True)
def orgs() -> None:
    store.ensure_local_environment_seeded()
    store.upsert_organization("local", "globo", "org-1", "Globo", True, "2026-01-01T00:00:00Z")
    store.upsert_organization("local", "acme", "org-2", "Acme", True, "2026-01-01T00:00:00Z")


def agent_row(org: str = "globo") -> store.Agent:
    sa = add_service_account(f"{org}-agent", org, role="NODE_AGENT")
    return store.add_agent("local", org, sa.id)


def path(org: str = "globo") -> Path:
    return Path.home() / "orgs" / org / "agent" / "data" / "results.log"


def row(i: int, org: str = "globo", detail: str | None = None) -> ResultRow:
    return ResultRow(T0 + timedelta(seconds=i), f"c-{i:04d}", "vl org start", org, i % 7 + 1, "dev-box",
                     "192.168.8.2", "STARTED" if i % 3 else "FAILED", None if i % 3 else 69,
                     detail if detail is not None else f"pid {1000 + i}")


def write(n: int, *, start: int = 0, org: str = "globo", max_bytes: int = 10_485_760, keep: int = 5) -> None:
    log = ResultsLog(path(org), max_bytes=max_bytes, keep=keep)
    for i in range(start, start + n):
        log.append(row(i, org))
    log.close()


def ids(text: str) -> list[str]:
    """The ``id`` column of the pipe-delimited lines (the header is not one)."""
    return [line.split("|")[1] for line in text.splitlines()[1:]]


def results(*args: str, env: dict[str, str] | None = None) -> Any:
    return runner.invoke(app, ["agent", "results", *args], env=env or WIDE)


def cid(i: int) -> str:
    return f"c-{i:04d}"


# --- not on a terminal: the file's own text -------------------------------------------


def test_default_is_the_header_then_the_last_20_newest_first() -> None:
    agent_row()
    write(30)
    result = results("globo")
    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines()[0] == HEADER
    assert ids(result.stdout) == [cid(i) for i in range(29, 9, -1)]


def test_the_lines_are_the_files_own_text() -> None:
    agent_row()
    log = ResultsLog(path(), max_bytes=10_485_760, keep=5)
    log.append(row(1, detail='a "quoted" | piped, detail'))
    log.close()
    result = results("globo")
    assert result.stdout.splitlines() == [HEADER, path().read_text().splitlines()[1]]


def test_limit() -> None:
    agent_row()
    write(30)
    assert ids(results("globo", "--limit", "5").stdout) == [cid(i) for i in range(29, 24, -1)]
    assert results("globo", "--limit", "0").exit_code == 2


def test_all_is_every_retained_result_newest_first_across_files() -> None:
    agent_row()
    write(200, max_bytes=4000)  # several rotations, 5 files kept
    files = resultslog.result_files(path())
    assert [p.name for p in files] == ["results.log"] + [f"results.log.{i}" for i in range(1, 5)]
    retained = sum(len(resultslog.read_file(p)) for p in files)
    got = ids(results("globo", "--all").stdout)
    assert len(got) == retained > 20
    assert got == sorted(got, reverse=True) and got[0] == cid(199)


@pytest.mark.parametrize("extra", [["--limit", "5"], ["--follow"]])
def test_all_with_limit_or_follow_is_a_usage_error(extra: list[str]) -> None:
    agent_row()
    write(3)
    assert results("globo", "--all", *extra).exit_code == 2


def test_a_short_current_file_is_filled_from_the_rotated_ones() -> None:
    agent_row()
    write(60, max_bytes=4000)
    current = resultslog.read_file(path())
    assert 0 < len(current) < 20
    assert ids(results("globo").stdout) == [cid(i) for i in range(59, 39, -1)]


def test_the_header_is_never_counted() -> None:
    agent_row()
    path().parent.mkdir(parents=True)
    path().write_text(HEADER_LINE)
    assert results("globo").stdout.splitlines() == [HEADER]
    Path(f"{path()}.1").write_text(HEADER_LINE + resultslog.encode_row(row(1)))
    assert ids(results("globo", "--limit", "1").stdout) == [cid(1)]


def test_it_reads_the_files_after_the_agent_has_stopped() -> None:
    store.set_agent_stopped(agent_row())
    write(3)
    assert ids(results("globo").stdout) == [cid(2), cid(1), cid(0)]


# --- the org omitted ------------------------------------------------------------------


def test_with_one_agent_the_org_can_be_omitted() -> None:
    agent_row("acme")
    write(2, org="acme")
    assert ids(results().stdout) == [cid(1), cid(0)]


def test_with_several_agents_the_org_must_be_named() -> None:
    agent_row("globo")
    agent_row("acme")
    result = results()
    assert result.exit_code == 1
    assert "More than one agent on this host: acme, globo. Name one: vl agent results acme" in result.output


def test_with_no_agent_it_points_at_create() -> None:
    result = results()
    assert result.exit_code == 1 and "vl agent create" in result.output


def test_a_named_org_with_no_agent_and_no_file_points_at_create() -> None:
    result = results("globo")
    assert result.exit_code == 1 and "vl agent create globo" in result.output


def test_a_named_org_with_files_but_no_agent_row_is_read() -> None:
    write(1)
    assert ids(results("globo").stdout) == [cid(0)]


# --- on a terminal --------------------------------------------------------------------


@pytest.fixture
def terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent_cmd, "is_terminal", lambda: True)


def _header_cells(output: str) -> list[str]:
    line = next(line for line in output.splitlines() if "COMMAND" in line)
    return [c.strip() for c in line.strip("┃│ ").replace("┃", "│").split("│")]


def test_a_terminal_gets_a_bordered_table_with_the_columns_in_order(terminal: None) -> None:
    agent_row()
    write(3)
    result = results("globo", env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.output
    assert _header_cells(result.output) == ["COMMAND", "NODE", "OUTCOME", "TIME", "ID", "DETAIL"]
    assert "globo/3" in result.output and "pid 1002" in result.output
    assert "2026-11-14T09:00:02.000Z" in result.output
    assert result.output.index(cid(2)) < result.output.index(cid(1))  # newest first
    assert "┌" in result.output or "┏" in result.output  # borders
    assert "too narrow" not in result.output


def test_the_column_widths(terminal: None) -> None:
    agent_row()
    write(1)
    output = results("globo", env={"COLUMNS": "200"}).output
    line = next(line for line in output.splitlines() if "COMMAND" in line)
    cells = line.split("│")[1:-1]
    assert [len(c) - 2 for c in cells[:5]] == [16, 14, 15, 24, 36]  # one space of padding each side


def test_a_narrow_terminal_leaves_detail_out_and_says_so(terminal: None) -> None:
    agent_row()
    write(3)
    result = results("globo", env={"COLUMNS": "120"})
    assert result.exit_code == 0
    assert "DETAIL" not in _header_cells(result.output)
    assert "pid 1002" not in result.output
    assert "Terminal too narrow for DETAIL (need 150, have 120): widen it, or use --details" in \
        " ".join(result.output.split())


def test_details_forces_detail_in(terminal: None) -> None:
    agent_row()
    write(3)
    result = results("globo", "--details", env={"COLUMNS": "120"})
    assert "DETAIL" in _header_cells(result.output) and "too narrow" not in result.output
    assert "pid" in result.output


def test_no_results_yet_on_a_terminal(terminal: None) -> None:
    agent_row()
    assert "No results yet" in results("globo").output


def test_json_output() -> None:
    agent_row()
    write(2)
    rows = json.loads(results("globo", env={"VL_OUTPUT": "json"}).stdout)
    assert [r["id"] for r in rows] == [cid(1), cid(0)]
    assert set(rows[0]) == set(resultslog.HEADER) and rows[0]["node"] == "2"


# --- --follow ---------------------------------------------------------------------------


class Ticks:
    """Stands in for the follow loop's sleep: runs a step on each tick, then Ctrl-C."""

    def __init__(self, steps: list[Any]) -> None:
        self.steps = steps

    def __call__(self, _seconds: float) -> None:
        if not self.steps:
            raise KeyboardInterrupt
        self.steps.pop(0)()


def test_follow_prints_the_backlog_in_time_order_then_new_results_across_a_rotation(
        monkeypatch: pytest.MonkeyPatch) -> None:
    agent_row()
    write(30)
    log = ResultsLog(path(), max_bytes=path().stat().st_size + 300, keep=5)
    steps = [lambda: log.append(row(30)), lambda: None, lambda: [log.append(row(i)) for i in (31, 32, 33)],
             lambda: log.append(row(34))]
    monkeypatch.setattr(agent_cmd.time, "sleep", Ticks(steps))

    result = results("globo", "--follow")

    log.close()
    assert result.exit_code == 0, result.output
    assert Path(f"{path()}.1").exists()  # it rotated while followed
    lines = result.stdout.splitlines()
    assert lines[0] == HEADER and lines.count(HEADER) == 1  # the new file's header is not repeated
    assert ids(result.stdout) == [cid(i) for i in range(10, 35)]  # last 20 in time order, then the rest


def test_follow_with_a_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    agent_row()
    write(60, max_bytes=4000)
    monkeypatch.setattr(agent_cmd.time, "sleep", Ticks([]))
    result = results("globo", "--limit", "50", "--follow")
    assert result.exit_code == 0
    assert ids(result.stdout) == [cid(i) for i in range(10, 60)]


def test_follow_on_a_terminal_is_plain_aligned_lines(monkeypatch: pytest.MonkeyPatch, terminal: None) -> None:
    agent_row()
    write(2)
    monkeypatch.setattr(agent_cmd.time, "sleep", Ticks([]))
    result = results("globo", "--follow", env={"COLUMNS": "80"})
    lines = result.stdout.splitlines()
    assert lines[0].split() == ["COMMAND", "NODE", "OUTCOME", "TIME", "ID", "DETAIL"]
    assert lines[1] == ("vl org start".ljust(16) + " " + "globo/1".ljust(14) + " " + "FAILED".ljust(15) + " " +
                        "2026-11-14T09:00:00.000Z".ljust(24) + " " + cid(0).ljust(36) + " pid 1000")
    assert not any(ch in result.stdout for ch in "│┃┌┏")  # no borders, not wrapped at 80


def test_follow_waits_for_a_file_that_does_not_exist_yet(monkeypatch: pytest.MonkeyPatch) -> None:
    agent_row()
    monkeypatch.setattr(agent_cmd.time, "sleep", Ticks([lambda: write(1)]))
    result = results("globo", "--follow")
    assert result.exit_code == 0 and ids(result.stdout) == [cid(0)]


# --- the reader itself ----------------------------------------------------------------------


def test_a_partly_written_line_is_held_back_until_its_newline(tmp_path: Path) -> None:
    p = tmp_path / "results.log"
    p.write_text(HEADER_LINE)
    follower = resultslog.Follower(p)
    assert follower.open_at_end() == []
    line = resultslog.encode_row(row(1))
    with open(p, "a") as f:
        f.write(line[:20])
    assert follower.poll() == []
    with open(p, "a") as f:
        f.write(line[20:])
    [got] = follower.poll()
    assert got.get("id") == cid(1) and got.raw == line.rstrip("\n")
    follower.close()


def test_parse_line_reads_quoted_fields() -> None:
    line = resultslog.parse_line(resultslog.encode_row(row(1, detail='x | "y"')))
    assert line is not None and line.get("detail") == 'x | "y"' and line.get("node") == "2"
    assert resultslog.parse_line(HEADER_LINE) is None and resultslog.parse_line("\n") is None
