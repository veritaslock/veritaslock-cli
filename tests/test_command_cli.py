"""`vl command show|watch|list` (vl-agent-cli-spec.md §8). The control plane is a respx stub."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest
import respx
from typer.testing import CliRunner

from _helpers import add_user
from test_wait import Clock, answer, target
from vl.app import app
from vl.commands import wait as wait_mod
from vl.lib import store

runner = CliRunner()
CP = "http://localhost:8082"
CID = "7f3a1c2e-5b6d-7a10-8e21-3c9d4f5a6b7c"
T0 = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)
WIDE = {"COLUMNS": "220"}


@pytest.fixture(autouse=True)
def world() -> None:
    store.ensure_local_environment_seeded()
    store.upsert_organization("local", "globo", "org-1", "Globo", True, "t")
    add_user("ops", [{"orgId": "org-1", "role": "ORG_ADMIN"}])


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    c = Clock()
    monkeypatch.setattr(wait_mod, "_now", c)
    monkeypatch.setattr(wait_mod, "_sleep", c.sleep)
    return c


def issued(cid: str = CID) -> None:
    store.record_issued_command("local", cid, "STOP", "ORG", "globo", None, "t", "t")


def cmd(*args: str, env: dict[str, str] = WIDE) -> Any:
    return runner.invoke(app, ["command", *args], env=env)


def every_state() -> dict[str, Any]:
    dto = answer("FAILED",
                 target("org-1", 1, "DONE", "STOPPED", "pid 4"),
                 target("org-1", 2, "FAILED", "FAILED", "still reported UP"),
                 target("org-1", 3, "NO_ANSWER", agent_state="STALE"),
                 {**target("org-1", 4, "SKIPPED", "SKIPPED", "NO_AGENT"), "agentId": None, "agentName": None},
                 target("org-1", 5, "PENDING"))
    return dto


# --- show ------------------------------------------------------------------------------------


@respx.mock
def test_show_has_the_header_counts_and_every_target_with_the_agents_state() -> None:
    respx.get(f"{CP}/v1/commands/{CID}").mock(return_value=httpx.Response(200, json=every_state()))
    respx.get(f"{CP}/v1/agents").mock(return_value=httpx.Response(200, json={"items": [
        {"id": "a-org-1", "name": "org-1-box", "host": "192.168.8.9", "lastSeenAt": "2026-11-14T11:58:00Z"}]}))
    result = cmd("show", CID)
    assert result.exit_code == 0, result.output
    out = " ".join(result.output.split())
    assert "vl org stop (STOP ORG globo) issued by ops at" in out
    assert "Status FAILED: 1 of 5 succeeded, 1 failed." in out
    for state in ("DONE", "FAILED", "NO_ANSWER", "SKIPPED", "PENDING"):
        assert state in result.output
    assert "STALE (last seen 2026-11-14T11:58:00Z)" in out
    assert "192.168.8.9" in out and "NO_AGENT" in out


@pytest.mark.parametrize("status, line", [
    ("SUCCEEDED", "1 of 1 succeeded."),
    ("INCOMPLETE", "0 of 1 succeeded; 1 did not answer."),
    ("PENDING", "0 of 1 succeeded; 1 not answered yet."),
])
@respx.mock
def test_show_each_overall_status(status: str, line: str) -> None:
    state = {"SUCCEEDED": "DONE", "INCOMPLETE": "NO_ANSWER", "PENDING": "PENDING"}[status]
    respx.get(f"{CP}/v1/commands/{CID}").mock(return_value=httpx.Response(
        200, json=answer(status, target("org-1", 1, state, "STOPPED" if state == "DONE" else None))))
    respx.get(f"{CP}/v1/agents").mock(return_value=httpx.Response(200, json={"items": []}))
    result = cmd("show", CID)
    assert result.exit_code == 0 and f"Status {status}: {line}" in " ".join(result.output.split())


@respx.mock
def test_show_last_is_the_newest_issued_command() -> None:
    issued("00000000-0000-0000-0000-000000000001")
    issued(CID)
    route = respx.get(f"{CP}/v1/commands/{CID}").mock(return_value=httpx.Response(200, json=every_state()))
    respx.get(f"{CP}/v1/agents").mock(return_value=httpx.Response(200, json={"items": []}))
    assert cmd("show", "--last").exit_code == 0 and route.call_count == 1


@respx.mock
def test_show_an_unknown_id_exits_1() -> None:
    respx.get(f"{CP}/v1/commands/{CID}").mock(return_value=httpx.Response(404, json={"detail": "Command not found."}))
    result = cmd("show", CID)
    assert result.exit_code == 1 and f"No command {CID}" in result.output


@respx.mock
def test_show_with_the_control_plane_unreachable_exits_4() -> None:
    respx.get(f"{CP}/v1/commands/{CID}").mock(side_effect=httpx.ConnectError("refused"))
    assert cmd("show", CID).exit_code == 4


def test_show_needs_an_id_or_last_not_both_and_last_needs_a_command() -> None:
    assert cmd("show").exit_code == 2
    assert cmd("show", CID, "--last").exit_code == 2
    none = cmd("show", "--last")
    assert none.exit_code == 1 and "No command has been issued" in none.output


@respx.mock
def test_show_json_is_the_control_planes_answer() -> None:
    respx.get(f"{CP}/v1/commands/{CID}").mock(return_value=httpx.Response(200, json=every_state()))
    respx.get(f"{CP}/v1/agents").mock(return_value=httpx.Response(200, json={"items": []}))
    result = cmd("show", CID, env={"VL_OUTPUT": "json"})
    assert json.loads(result.stdout)["counts"]["targets"] == 5


# --- watch -------------------------------------------------------------------------------------


@respx.mock
def test_watch_waits_like_the_remote_verbs_and_exits_from_the_final_answer(clock: Clock) -> None:
    issued()
    pending = answer("PENDING", target("org-1", 1, "DONE", "STOPPED"), target("org-1", 2, "PENDING"))
    pending["waitUntil"] = (T0 + timedelta(seconds=60)).isoformat()
    failed = answer("FAILED", target("org-1", 1, "DONE", "STOPPED"), target("org-1", 2, "FAILED", "FAILED"))
    respx.get(f"{CP}/v1/commands/{CID}").mock(side_effect=[httpx.Response(200, json=pending)] * 2 +
                                              [httpx.Response(200, json=failed)] * 2)
    result = cmd("watch", "--last")
    assert result.exit_code == 1, result.output
    assert "  globo/1" in result.output and "  globo/2" in result.output
    assert "Waiting on the control plane for: globo" in result.output


@respx.mock
def test_watch_an_explicit_timeout_that_runs_out_is_3(clock: Clock) -> None:
    still = answer("PENDING", target("org-1", 1, "PENDING"))
    still["waitUntil"] = (T0 + timedelta(seconds=60)).isoformat()
    route = respx.get(f"{CP}/v1/commands/{CID}").mock(return_value=httpx.Response(200, json=still))
    result = cmd("watch", CID, "--timeout", "3", "--interval", "1")
    assert result.exit_code == 3, result.output
    assert "The --timeout ran out before every target answered." in result.output
    assert clock.now <= T0 + timedelta(seconds=4)  # it did not wait for waitUntil
    assert route.call_count >= 3  # one poll a second, then the final answer, not asked again


@respx.mock
def test_watch_a_command_that_has_succeeded_exits_0(clock: Clock) -> None:
    done = answer("SUCCEEDED", target("org-1", 1, "DONE", "STOPPED"))
    respx.get(f"{CP}/v1/commands/{CID}").mock(return_value=httpx.Response(200, json=done))
    assert cmd("watch", CID).exit_code == 0


@respx.mock
def test_watch_unknown_and_unreachable() -> None:
    respx.get(f"{CP}/v1/commands/{CID}").mock(return_value=httpx.Response(404, json={"detail": "no"}))
    assert cmd("watch", CID).exit_code == 1
    respx.get(f"{CP}/v1/commands/{CID}").mock(side_effect=httpx.ConnectError("refused"))
    assert cmd("watch", CID).exit_code == 4


# --- list and history ---------------------------------------------------------------------------


@respx.mock
def test_list_newest_first_with_the_org_filter() -> None:
    route = respx.get(f"{CP}/v1/commands").mock(return_value=httpx.Response(200, json={"items": [
        {"correlationId": CID, "type": "STOP", "scope": "ORG", "orgId": "org-1", "issuedBy": "u-ops",
         "issuedAt": "2026-11-14T12:00:00Z", "status": "SUCCEEDED"},
        {"correlationId": "c-2", "type": "START", "scope": "NETWORK", "orgId": None, "issuedBy": "u-9",
         "issuedAt": "2026-11-14T11:00:00Z", "status": "FAILED"}], "nextCursor": None}))
    result = cmd("list", "--org", "globo", "--limit", "10", env={"VL_OUTPUT": "json"})
    assert result.exit_code == 0, result.output
    rows = json.loads(result.stdout)
    assert [(r["id"], r["org"], r["issuer"], r["status"]) for r in rows] == [
        (CID, "globo", "ops", "SUCCEEDED"), ("c-2", "-", "u-9", "FAILED")]
    assert dict(route.calls.last.request.url.params) == {"limit": "10", "orgId": "org-1"}


@respx.mock
def test_list_with_the_control_plane_unreachable_exits_4() -> None:
    respx.get(f"{CP}/v1/commands").mock(side_effect=httpx.ConnectError("refused"))
    assert cmd("list").exit_code == 4


def test_the_issued_command_history_keeps_the_newest_50() -> None:
    for i in range(55):
        issued(f"c-{i:02d}")
    rows = store.list_issued_commands("local")
    assert len(rows) == 50 and rows[0].correlation_id == "c-54" and rows[-1].correlation_id == "c-05"
