"""The wait (vl-agent-cli-spec.md §7.5) and its exit statuses (§7.6).

The control plane is a respx stub. A "running local agent" is a stand-in process
whose command line is the agent's; its results file is written with the agent's
own writer. The wait's clock and pause are driven by the test.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from typer.testing import CliRunner

from _helpers import add_service_account, add_user, kill_stand_in
from vl.agent.resultslog import ResultRow, ResultsLog
from vl.app import app
from vl.commands import wait as wait_mod
from vl.lib import proc, store

runner = CliRunner()
CP = "http://localhost:8082"
CID = "7f3a1c2e-5b6d-7a10-8e21-3c9d4f5a6b7c"
OTHER = "11111111-2222-3333-4444-555555555555"
T0 = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)
WIDE = {"COLUMNS": "200"}
ADMIN = [{"orgId": "org-1", "role": "ORG_ADMIN"}, {"orgId": "org-2", "role": "ORG_ADMIN"}]


@pytest.fixture(autouse=True)
def world() -> None:
    store.ensure_local_environment_seeded()
    store.upsert_organization("local", "globo", "org-1", "Globo", True, "t")
    store.upsert_organization("local", "acme", "org-2", "Acme", True, "t")
    add_user("ops", ADMIN)


@pytest.fixture
def stand_ins() -> Iterator[list[int]]:
    pids: list[int] = []
    yield pids
    for pid in pids:
        kill_stand_in(pid)


def running_agent(org: str, stand_ins: list[int]) -> Path:
    label = f"{org}-agent"
    sa = add_service_account(label, org, role="NODE_AGENT")
    agent = store.add_agent("local", org, sa.id)
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", "-m", "vl.agent", "--as", label])
    stand_ins.append(p.pid)
    deadline = time.monotonic() + 5
    while not proc.is_agent_process(p.pid, label) and time.monotonic() < deadline:
        time.sleep(0.02)
    store.set_agent_pid(agent, p.pid)
    return Path.home() / "orgs" / org / "agent" / "data" / "results.log"


class Clock:
    """The wait's clock: each pause moves it on and runs the next step, if any."""

    def __init__(self, steps: list[Any] | None = None) -> None:
        self.now = T0
        self.steps = steps or []
        self.pauses = 0

    def __call__(self) -> datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.pauses += 1
        self.now += timedelta(seconds=seconds)
        if self.steps:
            self.steps.pop(0)()


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    c = Clock()
    monkeypatch.setattr(wait_mod, "_now", c)
    monkeypatch.setattr(wait_mod, "_sleep", c.sleep)
    return c


def result(org: str, n: int, outcome: str = "STOPPED", cid: str = CID, detail: str = "pid 1") -> ResultRow:
    return ResultRow(T0, cid, "vl org stop", org, n, "dev-box", "192.168.8.2", outcome,
                     69 if outcome == "FAILED" else None, detail)


def write(path: Path, *rows: ResultRow, max_bytes: int = 10_485_760) -> None:
    log = ResultsLog(path, max_bytes=max_bytes, keep=5)
    for r in rows:
        log.append(r)
    log.close()


def accepted(*targets: tuple[str, int], wait_until: datetime = T0 + timedelta(seconds=60)) -> httpx.Response:
    return httpx.Response(202, json={
        "correlationId": CID, "expiresAt": (wait_until - timedelta(seconds=30)).isoformat(),
        "waitUntil": wait_until.isoformat(),
        "targets": [{"agentId": f"a-{o}", "agentName": f"{o}-box", "orgId": o, "orgNode": n} for o, n in targets],
        "skipped": []})


def target(org_id: str, n: int, state: str, outcome: str | None = None, detail: str = "",
           agent_state: str | None = None) -> dict[str, Any]:
    return {"orgId": org_id, "orgNode": n, "agentId": f"a-{org_id}", "agentName": f"{org_id}-box",
            "state": state, "outcome": outcome, "detail": detail, "exitCode": 69 if outcome == "FAILED" else None,
            "completedAt": None if outcome is None else T0.isoformat(), "receivedAt": None, "agentState": agent_state}


def answer(status: str, *targets: dict[str, Any]) -> dict[str, Any]:
    states = [t["state"] for t in targets]
    return {"correlationId": CID, "type": "STOP", "scope": "ORG", "orgId": "org-1", "orgNode": None,
            "command": "vl org stop", "options": {}, "issuedBy": "u-ops", "issuedAt": T0.isoformat(),
            "expiresAt": (T0 + timedelta(seconds=30)).isoformat(),
            "waitUntil": (T0 + timedelta(seconds=60)).isoformat(), "status": status,
            "counts": {"targets": len(states), "done": states.count("DONE"), "failed": states.count("FAILED"),
                       "noAnswer": states.count("NO_ANSWER"), "skipped": states.count("SKIPPED"),
                       "pending": states.count("PENDING")},
            "targets": list(targets)}


def stop(org: str = "globo") -> Any:
    return runner.invoke(app, ["org", "stop", org, "--remote"], env=WIDE)


# --- results from the local agent's file ---------------------------------------------------


@respx.mock
def test_results_are_printed_as_they_arrive_from_the_local_agents_file(clock: Clock, stand_ins: list[int]) -> None:
    path = running_agent("globo", stand_ins)
    write(path, result("globo", 9, cid=OTHER))  # an older command's row, in the file already
    respx.post(f"{CP}/v1/orgs/org-1/commands").mock(return_value=accepted(("org-1", 2), ("org-1", 1)))
    final = respx.get(f"{CP}/v1/commands/{CID}").mock(return_value=httpx.Response(200, json=answer(
        "SUCCEEDED", target("org-1", 1, "DONE", "STOPPED"), target("org-1", 2, "DONE", "STOPPED"))))
    clock.steps = [lambda: write(path, result("globo", 2), result("globo", 3, cid=OTHER)),
                   lambda: write(path, result("globo", 1, "ALREADY_STOPPED", detail="not running"))]

    out = stop()

    assert out.exit_code == 0, out.output
    lines = [line for line in out.output.splitlines() if line.startswith("  globo/")]
    assert lines == [wait_mod.stream_line("globo/2", "STOPPED", "2026-11-14T12:00:00.000Z", "pid 1"),
                     wait_mod.stream_line("globo/1", "ALREADY_STOPPED", "2026-11-14T12:00:00.000Z",
                                          "not running")]
    assert "Waiting on the control plane" not in out.output
    assert final.call_count == 1  # only the final answer: the file was the source
    assert "2 of 2 succeeded." in out.output and f"See: vl command show {CID}" in out.output


@respx.mock
def test_a_result_written_at_once_is_not_missed(clock: Clock, stand_ins: list[int]) -> None:
    path = running_agent("globo", stand_ins)
    write(path, result("globo", 9, cid=OTHER))

    def publish(request: httpx.Request) -> httpx.Response:
        write(path, result("globo", 1))  # the agent answers before the 202 is read
        return accepted(("org-1", 1))

    respx.post(f"{CP}/v1/orgs/org-1/commands").mock(side_effect=publish)
    respx.get(f"{CP}/v1/commands/{CID}").mock(return_value=httpx.Response(200, json=answer(
        "SUCCEEDED", target("org-1", 1, "DONE", "STOPPED"))))
    out = stop()
    assert out.exit_code == 0 and "  globo/1" in out.output and "globo/9" not in out.output
    assert clock.pauses == 0  # answered at once: no waiting


@respx.mock
def test_a_rotation_during_the_wait_is_followed(clock: Clock, stand_ins: list[int]) -> None:
    path = running_agent("globo", stand_ins)
    write(path, *[result("globo", 9, cid=OTHER) for _ in range(5)])
    size = path.stat().st_size
    respx.post(f"{CP}/v1/orgs/org-1/commands").mock(return_value=accepted(("org-1", 1), ("org-1", 2)))
    respx.get(f"{CP}/v1/commands/{CID}").mock(return_value=httpx.Response(200, json=answer(
        "SUCCEEDED", target("org-1", 1, "DONE", "STOPPED"), target("org-1", 2, "DONE", "STOPPED"))))
    # The first row fills the file; the second rotates it to results.log.1 and starts a new one.
    clock.steps = [lambda: write(path, result("globo", 1), result("globo", 2), max_bytes=size + 200)]
    out = stop()
    assert Path(f"{path}.1").exists()
    assert out.exit_code == 0, out.output
    assert "  globo/1" in out.output and "  globo/2" in out.output


def test_a_rotation_between_recording_and_following_is_not_lost(tmp_path: Path) -> None:
    from vl.agent.resultslog import Follower
    import os
    path = tmp_path / "results.log"
    write(path, result("globo", 9, cid=OTHER))
    st = os.stat(path)
    write(path, result("globo", 1), result("globo", 2), max_bytes=st.st_size + 150)  # rotates after node 1
    found = Follower(path).open_from(st.st_ino, st.st_size)
    assert [(line.get("node"), line.get("id")) for line in found] == [("1", CID), ("2", CID)]


# --- results from the control plane ------------------------------------------------------------


@respx.mock
def test_an_org_with_no_running_agent_here_is_polled_and_the_choice_said_once(clock: Clock) -> None:
    respx.post(f"{CP}/v1/orgs/org-1/commands").mock(return_value=accepted(("org-1", 1), ("org-1", 2)))
    pending = answer("PENDING", target("org-1", 1, "DONE", "STOPPED"), target("org-1", 2, "PENDING"))
    done = answer("FAILED", target("org-1", 1, "DONE", "STOPPED"),
                  target("org-1", 2, "FAILED", "FAILED", "still reported UP by the control plane"))
    route = respx.get(f"{CP}/v1/commands/{CID}").mock(side_effect=[
        httpx.Response(200, json=pending), httpx.Response(200, json=pending),
        httpx.Response(200, json=done), httpx.Response(200, json=done)])

    out = stop()

    assert out.exit_code == 1, out.output
    assert out.output.count("Waiting on the control plane for: globo (no running agent for it on this host)") == 1
    assert out.output.count("  globo/1") == 1 and "  globo/2        FAILED" in out.output
    assert "1 of 2 succeeded, 1 failed. Run the same command again to retry the node that failed." in out.output
    assert "Targets that did not succeed" in out.output and "still reported UP" in out.output
    assert route.call_count == 4  # three polls, then the final answer
    assert clock.pauses >= 4  # 2 s between polls, in 0.5 s pauses


@respx.mock
def test_the_final_answer_decides_even_against_the_lines_printed(clock: Clock, stand_ins: list[int]) -> None:
    path = running_agent("globo", stand_ins)
    respx.post(f"{CP}/v1/orgs/org-1/commands").mock(return_value=accepted(("org-1", 1)))
    clock.steps = [lambda: write(path, result("globo", 1))]  # the file says STOPPED
    respx.get(f"{CP}/v1/commands/{CID}").mock(return_value=httpx.Response(200, json=answer(
        "FAILED", target("org-1", 1, "FAILED", "EXPIRED"))))  # the control plane decided otherwise
    out = stop()
    assert out.exit_code == 1 and "  globo/1        STOPPED" in out.output


# --- exit codes (§7.6) ---------------------------------------------------------------------------


def _five(states: list[tuple[str, str | None]]) -> list[dict[str, Any]]:
    return [target("org-1", i + 1, s, o) for i, (s, o) in enumerate(states)]


@pytest.mark.parametrize("status, states, code, closing", [
    ("SUCCEEDED", [("DONE", "STOPPED")] * 5, 0, "5 of 5 succeeded."),
    ("FAILED", [("DONE", "STOPPED")] * 4 + [("FAILED", "FAILED")], 1, "4 of 5 succeeded, 1 failed."),
    ("INCOMPLETE", [("DONE", "STOPPED")] * 4 + [("NO_ANSWER", None)], 3, "4 of 5 succeeded; 1 did not answer."),
    ("INCOMPLETE", [("DONE", "STOPPED")] * 4 + [("SKIPPED", "SKIPPED")], 3,
     "4 of 5 succeeded; 1 skipped (no agent)."),
    ("FAILED", [("DONE", "STOPPED")] * 3 + [("FAILED", "FAILED"), ("NO_ANSWER", None)], 1,
     "3 of 5 succeeded, 1 failed."),
])
@respx.mock
def test_the_exit_status_follows_the_final_answer(status: str, states: list[tuple[str, str | None]], code: int,
                                                  closing: str, clock: Clock) -> None:
    respx.post(f"{CP}/v1/orgs/org-1/commands").mock(return_value=accepted(
        *[("org-1", i + 1) for i in range(5)], wait_until=T0))  # waitUntil reached at once
    respx.get(f"{CP}/v1/commands/{CID}").mock(return_value=httpx.Response(200, json=answer(status, *_five(states))))
    out = stop()
    assert out.exit_code == code, out.output
    assert closing in out.output


@respx.mock
def test_giving_up_at_wait_until_reports_no_answer(clock: Clock) -> None:
    respx.post(f"{CP}/v1/orgs/org-1/commands").mock(return_value=accepted(
        ("org-1", 1), ("org-1", 2), wait_until=T0 + timedelta(seconds=6)))
    pending = answer("PENDING", target("org-1", 1, "DONE", "STOPPED"), target("org-1", 2, "PENDING"))
    final = answer("INCOMPLETE", target("org-1", 1, "DONE", "STOPPED"),
                   target("org-1", 2, "NO_ANSWER", agent_state="STALE"))
    calls: list[int] = []

    def answer_for(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, json=final if clock.now >= T0 + timedelta(seconds=6) else pending)

    respx.get(f"{CP}/v1/commands/{CID}").mock(side_effect=answer_for)
    out = stop()
    assert out.exit_code == 3, out.output
    assert "NO_ANSWER" in out.output and "STALE" in out.output and "1 of 2 succeeded; 1 did not answer." in out.output


@respx.mock
def test_still_pending_after_wait_until_is_asked_again_every_second_for_5_seconds_then_3(clock: Clock) -> None:
    respx.post(f"{CP}/v1/orgs/org-1/commands").mock(return_value=accepted(("org-1", 1), wait_until=T0))
    route = respx.get(f"{CP}/v1/commands/{CID}").mock(return_value=httpx.Response(200, json=answer(
        "PENDING", target("org-1", 1, "PENDING"))))
    before = clock.now
    out = stop()
    assert out.exit_code == 3, out.output
    # One poll during the wait (it ends at once), the final answer, and five more.
    assert route.call_count == 1 + 1 + 5
    assert clock.now - before == timedelta(seconds=5)
    assert "1 not answered yet" in out.output


@respx.mock
def test_pending_that_settles_within_the_retries_is_its_answer(clock: Clock) -> None:
    respx.post(f"{CP}/v1/orgs/org-1/commands").mock(return_value=accepted(("org-1", 1), wait_until=T0))
    pending = httpx.Response(200, json=answer("PENDING", target("org-1", 1, "PENDING")))
    done = httpx.Response(200, json=answer("SUCCEEDED", target("org-1", 1, "DONE", "STOPPED")))
    respx.get(f"{CP}/v1/commands/{CID}").mock(side_effect=[pending, pending, pending, done])
    assert stop().exit_code == 0


@respx.mock
def test_the_control_plane_unreachable_at_the_final_answer_is_4_even_after_lines(clock: Clock,
                                                                                stand_ins: list[int]) -> None:
    path = running_agent("globo", stand_ins)
    respx.post(f"{CP}/v1/orgs/org-1/commands").mock(return_value=accepted(("org-1", 1)))
    clock.steps = [lambda: write(path, result("globo", 1))]
    respx.get(f"{CP}/v1/commands/{CID}").mock(side_effect=httpx.ConnectError("refused"))
    out = stop()
    assert out.exit_code == 4, out.output
    assert "  globo/1" in out.output  # the line was printed before
    flat = " ".join(out.output.split())
    assert "the outcome could not be confirmed (1 result seen above)" in flat and CID in flat


@respx.mock
def test_the_control_plane_unreachable_when_issuing_is_4() -> None:
    respx.post(f"{CP}/v1/orgs/org-1/commands").mock(side_effect=httpx.ConnectError("refused"))
    out = stop()
    assert out.exit_code == 4 and "Whether it was published is not known" in " ".join(out.output.split())
    assert store.list_issued_commands("local") == []


@respx.mock
def test_ctrl_c_stops_waiting_exits_130_and_leaves_the_command_running(clock: Clock) -> None:
    respx.post(f"{CP}/v1/orgs/org-1/commands").mock(return_value=accepted(("org-1", 1)))
    route = respx.get(f"{CP}/v1/commands/{CID}").mock(return_value=httpx.Response(200, json=answer(
        "PENDING", target("org-1", 1, "PENDING"))))

    def interrupt() -> None:
        raise KeyboardInterrupt

    clock.steps = [interrupt]
    out = stop()
    assert out.exit_code == 130, out.output
    assert f"the command keeps running. Command {CID}. See: vl command show {CID}" in " ".join(out.output.split())
    assert route.call_count == 1  # the poll before Ctrl-C; no final answer, nothing cancelled
    assert not any(c.request.method == "DELETE" for c in respx.calls)


def test_exit_status_order() -> None:
    from vl.commands import exits
    assert exits.exit_status("FAILED", unreachable=True) == 4
    assert [exits.exit_status(s) for s in ("FAILED", "INCOMPLETE", "PENDING", "SUCCEEDED", None)] == [1, 3, 3, 0, 3]
