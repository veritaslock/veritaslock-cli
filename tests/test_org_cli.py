"""Tests for the `vl org` command group (HTTP mocked with respx)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import respx
from typer.testing import CliRunner

from _helpers import fake_jwt
from vl.app import app
from vl.lib import store

runner = CliRunner()
IDP = "http://localhost:8080"
# Decodable, so commands that check the caller's role can read it.
CALLER_JWT = fake_jwt("u-root", typ="user", orgs=[{"orgId": "org-1", "role": "ORG_ADMIN"}])

ORG_DTO = {
    "id": "org-1",
    "name": "globo",
    "displayName": "Globo Corp",
    "active": True,
    "createdAt": "2026-01-01T00:00:00Z",
    "createdBy": "u-0",
    "owner": "u-0",
}


def _caller() -> store.Identity:
    """A resolved-and-tokened default identity so write commands skip re-auth."""
    store.ensure_local_environment_seeded()
    ident = store.add_identity("local", "USER", "u-root", "root", "root")
    store.set_user_acct(ident.id, "pw")
    store.set_default_identity("local", "root")
    now = datetime.now(timezone.utc)
    store.set_cached_token(ident.id, CALLER_JWT, now, now + timedelta(hours=1))
    return ident


def _mock_org() -> None:
    respx.get(f"{IDP}/v1/organizations", params={"name": "globo"}).mock(
        return_value=httpx.Response(200, json=ORG_DTO)
    )


# --------------------------------------------------------------------------- #
# reads (unauthenticated)
# --------------------------------------------------------------------------- #


@respx.mock
def test_show_hits_server_and_caches() -> None:
    _mock_org()
    result = runner.invoke(app, ["org", "show", "globo"], env={"VL_OUTPUT": "json"})
    assert result.exit_code == 0, result.stdout
    assert "org-1" in result.stdout
    assert store.get_organization("local", "globo").display_name == "Globo Corp"


@respx.mock
def test_list_caches_every_row_and_shows_paging_hint() -> None:
    respx.get(f"{IDP}/v1/organizations").mock(
        return_value=httpx.Response(200, json={"items": [ORG_DTO], "nextCursor": "1"})
    )
    result = runner.invoke(app, ["org", "list"])
    assert result.exit_code == 0, result.stdout
    assert "--page 1" in result.stdout
    assert store.list_organizations("local")[0].name == "globo"


# --------------------------------------------------------------------------- #
# writes (authenticated as the resolved identity)
# --------------------------------------------------------------------------- #


@respx.mock
def test_add_creates_and_caches() -> None:
    _caller()
    create = respx.post(f"{IDP}/v1/organizations").mock(
        return_value=httpx.Response(201, json=ORG_DTO)
    )

    result = runner.invoke(app, ["org", "add", "globo", "--display-name", "Globo Corp"])

    assert result.exit_code == 0, result.stdout
    body = create.calls.last.request.content
    assert b'"name":"globo"' in body and b'"displayName":"Globo Corp"' in body
    assert b"initialAdminUserId" not in body
    assert create.calls.last.request.headers["authorization"] == f"Bearer {CALLER_JWT}"
    assert store.get_organization("local", "globo").server_org_id == "org-1"


@respx.mock
def test_add_with_initial_admin() -> None:
    _caller()
    create = respx.post(f"{IDP}/v1/organizations").mock(
        return_value=httpx.Response(201, json=ORG_DTO)
    )

    result = runner.invoke(
        app,
        ["org", "add", "globo", "--display-name", "Globo", "--initial-admin", "u-42"],
    )

    assert result.exit_code == 0, result.stdout
    assert b'"initialAdminUserId":"u-42"' in create.calls.last.request.content


@respx.mock
def test_add_surfaces_server_conflict_cleanly() -> None:
    _caller()
    respx.post(f"{IDP}/v1/organizations").mock(
        return_value=httpx.Response(
            409, json={"detail": "Organization name already exists.", "errorCode": "CONFLICT"}
        )
    )

    result = runner.invoke(app, ["org", "add", "globo", "--display-name", "Globo"])

    assert result.exit_code == 1
    assert "already exists" in result.stdout
    assert "Traceback" not in result.stdout


def test_add_with_no_identity_errors() -> None:
    store.ensure_local_environment_seeded()
    result = runner.invoke(app, ["org", "add", "globo", "--display-name", "Globo"])
    assert result.exit_code == 1
    assert "No identity selected" in result.stdout


def test_update_requires_a_field() -> None:
    result = runner.invoke(app, ["org", "update", "globo"])
    assert result.exit_code == 1
    assert "nothing to update" in result.stdout


@respx.mock
def test_update_patches_by_name() -> None:
    _caller()
    patch = respx.patch(f"{IDP}/v1/organizations", params={"name": "globo"}).mock(
        return_value=httpx.Response(200, json={**ORG_DTO, "active": False})
    )

    result = runner.invoke(
        app, ["org", "update", "globo", "--no-active"], env={"VL_OUTPUT": "json"}
    )

    assert result.exit_code == 0, result.stdout
    assert b'"active":false' in patch.calls.last.request.content



# --------------------------------------------------------------------------- #
# start / stop / reset: ORG_ADMIN of the org or PLATFORM_ADMIN, checked first,
# then passed down to every node command (vl-authorization-spec.md §3.5)
# --------------------------------------------------------------------------- #

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from _helpers import add_service_account, add_user
from vl.commands import org as org_cmd
from vl.commands._shared import CliError, report_errors

WIDE = {"COLUMNS": "200"}


class NodeCalls:
    """Records the node commands org start/stop/reset make, stop's DOWN polls,
    reset's state checks (each node reports ``states.get(n, "DOWN")``), the
    signals sent, and the order checks and prompts happen in (``events``)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int, dict[str, Any]]] = []
        self.polled: list[int] = []
        self.checked: list[int] = []
        self.states: dict[int, str] = {}
        self.signals: list[tuple[int, int]] = []
        self.events: list[str] = []

    def recorder(self, verb: str) -> Any:
        def call(org_name: str, n: int, **kwargs: Any) -> bool:
            self.calls.append((verb, n, kwargs))
            return True  # node.reset: it deleted the node's data
        return call


@pytest.fixture
def node_calls(monkeypatch: pytest.MonkeyPatch) -> NodeCalls:
    nc = NodeCalls()
    for verb in ("start", "stop", "reset"):
        monkeypatch.setattr(org_cmd.node, verb, nc.recorder(verb))

    def node_is_up(environment: store.Environment, node_: store.Node) -> bool:
        nc.polled.append(node_.org_node)  # as the node's own service account, not the operator
        return False

    def fetch_node_by_id(environment: store.Environment, node_: store.Node) -> dict[str, Any]:
        nc.checked.append(node_.org_node)
        nc.events.append(f"check {node_.org_node}")
        return {"state": nc.states.get(node_.org_node, "DOWN")}

    monkeypatch.setattr(org_cmd.node, "node_is_up", node_is_up)
    monkeypatch.setattr(org_cmd.node, "fetch_node_by_id", fetch_node_by_id)
    monkeypatch.setattr(org_cmd.node.os, "kill", lambda pid, sig: nc.signals.append((pid, sig)))
    monkeypatch.setattr(org_cmd.time, "sleep", lambda s: None)
    return nc


def _globo_with_nodes(count: int = 2, *, new: set[int] | None = None) -> None:
    """``count`` globo nodes, all NEW unless ``new`` is given: then every node
    not in it has run once (STOPPED), so org reset checks its state."""
    store.ensure_local_environment_seeded()
    store.upsert_organization("local", "globo", "org-1", "Globo", True, "2026-01-01T00:00:00Z")
    store.upsert_organization("local", "acme", "org-2", "Acme", True, "2026-01-01T00:00:00Z")
    for n in range(1, count + 1):
        sa = store.add_identity("local", "SERVICE_ACCOUNT", f"sa-{n}", f"globo-{n}", f"globo-{n}")
        store.set_svc_acct(sa.id, "local", "globo", f"client-{n}", "secret", role="NODE")
        row = store.add_node("local", "globo", n, sa.id, 7000 + n)
        if new is not None and n not in new:
            store.set_node_stopped(store.set_pid(row, 4300 + n))


ORG_COMMANDS = {
    "start": ["org", "start", "globo"],
    "stop": ["org", "stop", "globo"],
    "reset": ["org", "reset", "globo", "--yes"],
}
NODE_VERBS = {"start": {"start"}, "stop": {"stop"}, "reset": {"reset"}}


@pytest.mark.parametrize("command", list(ORG_COMMANDS))
@pytest.mark.parametrize(
    "orgs",
    [[{"orgId": "org-1", "role": "ORG_ADMIN"}], [{"orgId": "org-2", "role": "PLATFORM_ADMIN"}]],
    ids=["org-admin", "platform-admin"],
)
def test_org_command_is_allowed_and_passes_the_identity_to_every_node_call(
    command: str, orgs: list, node_calls: NodeCalls
) -> None:
    _globo_with_nodes()
    add_user("ops", orgs)

    result = runner.invoke(app, ORG_COMMANDS[command], env=WIDE)

    assert result.exit_code == 0, result.output
    assert {verb for verb, _, _ in node_calls.calls} == NODE_VERBS[command]
    assert {n for _, n, _ in node_calls.calls} == {1, 2}
    assert all(kw["as_"] == "ops" and kw["env"] == "local" for _, _, kw in node_calls.calls)


def test_org_command_passes_the_as_identity_not_the_default(node_calls: NodeCalls) -> None:
    _globo_with_nodes()
    add_user("ops", [{"orgId": "org-1", "role": "USER"}])
    add_user("boss", [{"orgId": "org-1", "role": "ORG_ADMIN"}], default=False)

    result = runner.invoke(app, [*ORG_COMMANDS["stop"], "--as", "boss"], env=WIDE)

    assert result.exit_code == 0, result.output
    assert [kw["as_"] for _, _, kw in node_calls.calls] == ["boss", "boss"]


def test_org_reset_passes_include_snapshots_down(node_calls: NodeCalls) -> None:
    _globo_with_nodes()
    add_user("ops", [{"orgId": "org-1", "role": "ORG_ADMIN"}])

    runner.invoke(app, [*ORG_COMMANDS["reset"], "--include-snapshots"], env=WIDE)

    resets = [kw for verb, _, kw in node_calls.calls if verb == "reset"]
    assert resets and all(kw["include_snapshots"] is True and kw["yes"] is True for kw in resets)


def test_org_reset_without_include_snapshots_passes_false_down(node_calls: NodeCalls) -> None:
    _globo_with_nodes()
    add_user("ops", [{"orgId": "org-1", "role": "ORG_ADMIN"}])

    result = runner.invoke(app, ORG_COMMANDS["reset"], env=WIDE)

    assert result.exit_code == 0, result.output
    resets = [kw for verb, _, kw in node_calls.calls if verb == "reset"]
    assert resets and all(kw["include_snapshots"] is False and kw["yes"] is True for kw in resets)


def test_org_stop_still_polls_as_each_nodes_own_account(node_calls: NodeCalls) -> None:
    _globo_with_nodes()
    add_user("ops", [{"orgId": "org-1", "role": "ORG_ADMIN"}])

    runner.invoke(app, ORG_COMMANDS["stop"], env=WIDE)

    assert node_calls.polled == [2, 1]  # descending, one DOWN poll per node


ORG_REFUSALS = {
    "user": [{"orgId": "org-1", "role": "USER"}],
    "key-reader": [{"orgId": "org-1", "role": "KEY_READER"}],
    "admin-of-another-org": [{"orgId": "org-2", "role": "ORG_ADMIN"}],
}


@pytest.mark.parametrize("command", list(ORG_COMMANDS))
@pytest.mark.parametrize("who", list(ORG_REFUSALS))
def test_org_command_refusal_calls_no_node_command(command: str, who: str, node_calls: NodeCalls) -> None:
    _globo_with_nodes()
    add_user("ops", ORG_REFUSALS[who])

    result = runner.invoke(app, ORG_COMMANDS[command], env=WIDE)

    assert result.exit_code == 1
    assert "'ops' is not an admin of 'globo'" in result.output
    assert node_calls.calls == [] and node_calls.polled == [] and node_calls.checked == []


@pytest.mark.parametrize("command", list(ORG_COMMANDS))
@pytest.mark.parametrize("role", ["NODE", "SYSTEM"])
def test_org_command_is_refused_for_a_service_account(command: str, role: str, node_calls: NodeCalls) -> None:
    _globo_with_nodes()
    add_service_account("ops-sa", "globo", role)

    result = runner.invoke(app, [*ORG_COMMANDS[command], "--as", "ops-sa"], env=WIDE)

    assert result.exit_code == 1
    assert "service accounts can't run this command" in result.output
    assert node_calls.calls == []


@pytest.mark.parametrize("command", list(ORG_COMMANDS))
def test_org_command_without_an_identity_is_refused(command: str, node_calls: NodeCalls) -> None:
    _globo_with_nodes()

    result = runner.invoke(app, ORG_COMMANDS[command], env=WIDE)

    assert result.exit_code == 1
    assert "No identity selected" in result.output
    assert node_calls.calls == []


def test_org_command_on_an_uncached_org_keeps_the_existing_error(node_calls: NodeCalls) -> None:
    _globo_with_nodes()
    add_user("ops", [{"orgId": "org-1", "role": "PLATFORM_ADMIN"}])

    result = runner.invoke(app, ["org", "start", "zeta"], env=WIDE)

    assert result.exit_code == 1
    assert "vl org show zeta" in result.output
    assert node_calls.calls == []


@pytest.fixture
def tty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(org_cmd, "sys", SimpleNamespace(stdin=SimpleNamespace(isatty=lambda: True)))


def test_org_reset_refusal_never_shows_the_prompt(tty: None, node_calls: NodeCalls) -> None:
    _globo_with_nodes()
    add_user("ops", [{"orgId": "org-1", "role": "USER"}])

    result = runner.invoke(app, ["org", "reset", "globo"], input="y\n", env=WIDE)

    assert result.exit_code == 1
    assert "Are you sure" not in result.output
    assert node_calls.calls == []


def test_org_reset_prompt_still_works_after_the_check(tty: None, node_calls: NodeCalls) -> None:
    _globo_with_nodes(new=set())
    add_user("ops", [{"orgId": "org-1", "role": "ORG_ADMIN"}])

    declined = runner.invoke(app, ["org", "reset", "globo"], input="n\n", env=WIDE)
    assert declined.exit_code == 0 and "Are you sure" in declined.output
    assert node_calls.calls == []

    confirmed = runner.invoke(app, ["org", "reset", "globo"], input="y\n", env=WIDE)
    assert confirmed.exit_code == 0, confirmed.output
    assert {verb for verb, _, _ in node_calls.calls} == {"reset"}


# --------------------------------------------------------------------------- #
# org reset: every node must already be DOWN; it stops nothing
# --------------------------------------------------------------------------- #


def test_org_reset_refuses_naming_every_node_that_is_not_down(node_calls: NodeCalls) -> None:
    _globo_with_nodes(3, new=set())
    add_user("ops", [{"orgId": "org-1", "role": "ORG_ADMIN"}])
    node_calls.states = {1: "UP", 3: "STARTING"}

    result = runner.invoke(app, ORG_COMMANDS["reset"], env=WIDE)

    assert result.exit_code == 1
    assert "Can't reset org 'globo': every node must be down, and these are not:" in result.output
    assert "globo/node3 (STARTING)" in result.output and "globo/node1 (UP)" in result.output
    assert "globo/node2" not in result.output
    assert "Run `vl org stop globo` first." in result.output
    assert node_calls.checked == [3, 2, 1]  # one check per node, no poll
    assert node_calls.calls == [] and node_calls.polled == [] and node_calls.signals == []


def test_org_reset_refuses_when_only_one_of_several_nodes_is_up(node_calls: NodeCalls) -> None:
    _globo_with_nodes(3, new=set())
    add_user("ops", [{"orgId": "org-1", "role": "ORG_ADMIN"}])
    node_calls.states = {2: "UP"}

    result = runner.invoke(app, ORG_COMMANDS["reset"], env=WIDE)

    assert result.exit_code == 1
    assert "globo/node2 (UP)" in result.output
    assert node_calls.calls == [] and node_calls.signals == []


def test_org_reset_resets_every_node_when_all_are_down(node_calls: NodeCalls) -> None:
    _globo_with_nodes(3, new=set())
    add_user("ops", [{"orgId": "org-1", "role": "ORG_ADMIN"}])

    result = runner.invoke(app, ORG_COMMANDS["reset"], env=WIDE)

    assert result.exit_code == 0, result.output
    assert node_calls.checked == [3, 2, 1]
    assert [(verb, n) for verb, n, _ in node_calls.calls] == [("reset", 3), ("reset", 2), ("reset", 1)]
    assert node_calls.polled == [] and node_calls.signals == []


def test_org_reset_does_not_check_new_nodes(node_calls: NodeCalls) -> None:
    _globo_with_nodes(3, new={1, 2})
    add_user("ops", [{"orgId": "org-1", "role": "ORG_ADMIN"}])
    node_calls.states = {1: "UP", 2: "UP"}  # never asked: a NEW node has never registered

    result = runner.invoke(app, ORG_COMMANDS["reset"], env=WIDE)

    assert result.exit_code == 0, result.output
    assert node_calls.checked == [3]
    assert [n for _, n, _ in node_calls.calls] == [3, 2, 1]


def test_org_reset_role_check_comes_before_the_state_check(node_calls: NodeCalls) -> None:
    _globo_with_nodes(2, new=set())
    add_user("ops", [{"orgId": "org-1", "role": "USER"}])
    node_calls.states = {1: "UP"}

    result = runner.invoke(app, ORG_COMMANDS["reset"], env=WIDE)

    assert result.exit_code == 1
    assert "'ops' is not an admin of 'globo'" in result.output
    assert "not down" not in result.output
    assert node_calls.checked == [] and node_calls.calls == []


def test_org_reset_prompts_once_after_the_check(
    tty: None, node_calls: NodeCalls, monkeypatch: pytest.MonkeyPatch
) -> None:
    _globo_with_nodes(2, new=set())
    add_user("ops", [{"orgId": "org-1", "role": "ORG_ADMIN"}])

    def confirm(text: str, **_: Any) -> bool:
        node_calls.events.append("prompt")
        return True

    monkeypatch.setattr(org_cmd.typer, "confirm", confirm)
    result = runner.invoke(app, ["org", "reset", "globo"], env=WIDE)

    assert result.exit_code == 0, result.output
    assert node_calls.events == ["check 2", "check 1", "prompt"]
    assert [n for _, n, _ in node_calls.calls] == [2, 1]


def test_org_reset_prompt_is_never_shown_when_the_check_refuses(tty: None, node_calls: NodeCalls) -> None:
    _globo_with_nodes(2, new=set())
    add_user("ops", [{"orgId": "org-1", "role": "ORG_ADMIN"}])
    node_calls.states = {1: "UP"}

    result = runner.invoke(app, ["org", "reset", "globo"], input="y\n", env=WIDE)

    assert result.exit_code == 1
    assert "Are you sure" not in result.output
    assert node_calls.calls == []


@pytest.mark.parametrize(
    ("flags", "prompt"),
    [
        ([], "Reset ALL nodes in org 'globo'? This permanently deletes all local blockchain state. "
             "Are you sure (y/N)? "),
        (["--include-snapshots"],
         "Reset the data and snapshots directories of ALL nodes in org 'globo'? "
         "This permanently deletes all local blockchain state. Are you sure (y/N)? "),
    ],
    ids=["data", "data-and-snapshots"],
)
def test_org_reset_prompt_wording(flags: list[str], prompt: str, tty: None, node_calls: NodeCalls) -> None:
    _globo_with_nodes()
    add_user("ops", [{"orgId": "org-1", "role": "ORG_ADMIN"}])

    result = runner.invoke(app, ["org", "reset", "globo", *flags], input="n\n", env=WIDE)

    assert result.exit_code == 0, result.output
    assert result.output.startswith(prompt)
    assert result.output.count("Are you sure") == 1
    assert node_calls.calls == []


def test_org_reset_without_a_tty_or_yes_refuses_after_the_check(node_calls: NodeCalls) -> None:
    _globo_with_nodes(2, new=set())
    add_user("ops", [{"orgId": "org-1", "role": "ORG_ADMIN"}])

    result = runner.invoke(app, ["org", "reset", "globo"], env=WIDE)

    assert result.exit_code == 1
    assert "vl org reset requires confirmation; pass --yes to run non-interactively" in result.output
    assert node_calls.checked == [2, 1] and node_calls.calls == []


def _node_data(home: Path, n: int) -> Path:
    data = home / "orgs" / "globo" / "nodes" / f"node{n}" / "data"
    data.mkdir(parents=True, exist_ok=True)
    (data / "chain.db").write_text("blocks")
    return data


def test_org_reset_reports_a_node_that_came_up_after_the_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real node.reset: its own check skips node2, which came up between
    org reset's check and its turn; that is reported, and the exit is non-zero."""
    monkeypatch.setenv("HOME", str(tmp_path))
    _globo_with_nodes(3, new=set())
    add_user("ops", [{"orgId": "org-1", "role": "ORG_ADMIN"}])
    data = {n: _node_data(tmp_path, n) for n in (1, 2, 3)}
    checks: list[int] = []

    def fetch_node_by_id(environment: store.Environment, node_: store.Node) -> dict[str, Any]:
        checks.append(node_.org_node)
        came_up = node_.org_node == 2 and checks.count(2) > 1
        return {"state": "UP" if came_up else "DOWN"}

    monkeypatch.setattr(org_cmd.node, "fetch_node_by_id", fetch_node_by_id)
    result = runner.invoke(app, ORG_COMMANDS["reset"], env=WIDE)

    assert result.exit_code == 1
    assert "1 of 3 nodes in globo were not reset:" in result.output
    assert "globo/node2: skipped: reported UP by the control plane when its turn came" in result.output
    assert (data[2] / "chain.db").exists()
    assert not (data[1] / "chain.db").exists() and not (data[3] / "chain.db").exists()


def test_org_reset_reports_a_node_whose_reset_failed_and_resets_the_rest(
    node_calls: NodeCalls, monkeypatch: pytest.MonkeyPatch
) -> None:
    _globo_with_nodes(3, new=set())
    add_user("ops", [{"orgId": "org-1", "role": "ORG_ADMIN"}])
    resets: list[int] = []

    def reset(org_name: str, n: int, **_: Any) -> bool:
        resets.append(n)
        with report_errors():  # as the real command does
            if n == 2:
                raise CliError("disk on fire")
        return True

    monkeypatch.setattr(org_cmd.node, "reset", reset)
    result = runner.invoke(app, ORG_COMMANDS["reset"], env=WIDE)

    assert result.exit_code == 1
    assert resets == [3, 2, 1]
    assert "1 of 3 nodes in globo were not reset:" in result.output
    assert "globo/node2: disk on fire" in result.output


# --------------------------------------------------------------------------- #
# org start: a node that fails to start is reported, the rest still start
# --------------------------------------------------------------------------- #

import typer

from vl.commands import node as node_cmd


def _starts(monkeypatch: pytest.MonkeyPatch, module: Any, outcomes: dict[tuple[str, int], str]) -> list[tuple[str, int]]:
    """Replace node.start: each (org, n) in ``outcomes`` ends as told ("sigabrt",
    "running" or "error"); every other node starts. Returns the calls made."""
    calls: list[tuple[str, int]] = []

    def start(org_name: str, n: int, **_: Any) -> None:
        calls.append((org_name, n))
        outcome = outcomes.get((org_name, n), "started")
        with report_errors():  # as the real command does
            if outcome == "sigabrt":
                raise node_cmd.NodeStartFailed(f"{org_name}/node{n}", -6, Path("/no/such.out"))
            if outcome == "error":
                raise CliError("No such file: verilock")
            if outcome == "running":
                print(f"{org_name} node{n} is already running (pid 4312)")
            else:
                print(f"Started {org_name}/node{n} (pid: 100{n}).")

    monkeypatch.setattr(module.node, "start", start)
    return calls


def test_org_start_reports_a_failed_node_and_starts_the_rest(monkeypatch: pytest.MonkeyPatch) -> None:
    _globo_with_nodes(3)
    add_user("ops", [{"orgId": "org-1", "role": "ORG_ADMIN"}])
    calls = _starts(monkeypatch, org_cmd, {("globo", 2): "sigabrt"})

    result = runner.invoke(app, ["org", "start", "globo"], env=WIDE)

    assert result.exit_code == 1
    assert calls == [("globo", 1), ("globo", 2), ("globo", 3)]
    out = result.output
    assert "Started globo/node1" in out and "Started globo/node3" in out
    assert "Started globo/node2" not in out
    # the node's own error as it happened, then the summary at the end
    assert "Error: globo/node2 exited right after starting (killed by SIGABRT)." in out
    assert out.rstrip().endswith(
        "Error: 1 of 3 nodes in globo failed to start:\n"
        "  globo/node2: exited right after starting (killed by SIGABRT)"
    )


def test_org_start_summarizes_every_failure_with_its_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    _globo_with_nodes(3)
    add_user("ops", [{"orgId": "org-1", "role": "ORG_ADMIN"}])
    _starts(monkeypatch, org_cmd, {("globo", 1): "error", ("globo", 3): "sigabrt"})

    result = runner.invoke(app, ["org", "start", "globo"], env=WIDE)

    assert result.exit_code == 1
    assert (
        "Error: 2 of 3 nodes in globo failed to start:\n"
        "  globo/node1: No such file: verilock\n"
        "  globo/node3: exited right after starting (killed by SIGABRT)"
    ) in result.output


def test_org_start_an_already_running_node_is_not_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    _globo_with_nodes(2)
    add_user("ops", [{"orgId": "org-1", "role": "ORG_ADMIN"}])
    _starts(monkeypatch, org_cmd, {("globo", 1): "running"})

    result = runner.invoke(app, ["org", "start", "globo"], env=WIDE)

    assert result.exit_code == 0, result.output
    assert "globo node1 is already running" in result.output
    assert "Started globo/node2" in result.output
    assert "Error" not in result.output and "failed" not in result.output
