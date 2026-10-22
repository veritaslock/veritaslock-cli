"""Tests for `vl network reset` (control plane mocked with respx).

The per-org work (`vl org reset`, which stops and resets real nodes) is replaced
with a recorder, so these tests cover only what `network reset` itself owns: the
control-plane dry run and real call, the prompt, error reporting, and calling
`org reset` per org in the right order.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import respx
from typer.testing import CliRunner

from _helpers import add_service_account, add_user, fake_jwt
from vl.app import app
from vl.commands import network
from vl.lib import store

runner = CliRunner()
CP = "http://localhost:8082"
RESET_URL = f"{CP}/v1/network/reset"
WIDE = {"COLUMNS": "200"}  # keep Rich from wrapping long error lines

# A decodable PLATFORM_ADMIN user token: the org resets network reset calls check it.
CALLER_JWT = fake_jwt(
    "u-root", typ="user", orgs=[{"orgId": "veritaslock", "role": "PLATFORM_ADMIN"}]
)

COUNTS = {"study_event_schema_ref": 0, "study": 3, "block": 40}


def _caller() -> store.Identity:
    """A resolved-and-tokened default identity so the command skips re-auth."""
    store.ensure_local_environment_seeded()
    ident = store.add_identity("local", "USER", "u-root", "root", "root")
    store.set_user_acct(ident.id, "pw")
    store.set_default_identity("local", "root")
    now = datetime.now(timezone.utc)
    store.set_cached_token(ident.id, CALLER_JWT, now, now + timedelta(hours=1))
    return ident


def _orgs(*names: str) -> None:
    for i, name in enumerate(names):
        store.upsert_organization(
            "local", name, f"org-{i}", name.title(), True, f"2026-01-0{i + 1}T00:00:00Z"
        )


@pytest.fixture
def org_resets(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    """Record each `org.reset` call instead of touching real nodes."""
    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_reset(org_name: str, **kwargs: Any) -> None:
        calls.append((org_name, kwargs))

    monkeypatch.setattr(network.org, "reset", fake_reset)
    return calls


@pytest.fixture
def tty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make `network reset` believe it is attached to a terminal.

    CliRunner swaps in its own non-TTY stdin during invoke, so the module's
    reference to `sys` is replaced rather than `sys.stdin` itself.
    """
    monkeypatch.setattr(
        network, "sys", SimpleNamespace(stdin=SimpleNamespace(isatty=lambda: True))
    )


def _ok(dry_run: bool, deleted: dict[str, int] = COUNTS) -> httpx.Response:
    return httpx.Response(200, json={"dryRun": dry_run, "deleted": deleted})


# --------------------------------------------------------------------------- #
# success
# --------------------------------------------------------------------------- #


@respx.mock
def test_yes_skips_dry_run_and_resets_cp_then_every_org(org_resets: list) -> None:
    _caller()
    _orgs("globo", "acme", "veritaslock", "zeta")
    dry = respx.post(RESET_URL, params={"dryRun": "true"}).mock(return_value=_ok(True))
    real = respx.post(RESET_URL, params={"dryRun": "false"}).mock(return_value=_ok(False))

    result = runner.invoke(app, ["network", "reset", "--yes"], env=WIDE)

    assert result.exit_code == 0, result.stdout
    assert not dry.called
    assert real.call_count == 1
    assert real.calls.last.request.headers["authorization"] == f"Bearer {CALLER_JWT}"
    # GLOBO last, veritaslock never; snapshots always included, never prompted.
    assert [name for name, _ in org_resets] == ["zeta", "acme", "globo"]
    assert all(kw["include_snapshots"] is True and kw["yes"] is True for _, kw in org_resets)
    assert all(kw["as_"] == "root" and kw["env"] == "local" for _, kw in org_resets)


@respx.mock
def test_confirmed_prompt_dry_runs_first_and_shows_counts(tty: None, org_resets: list) -> None:
    _caller()
    _orgs("globo")
    dry = respx.post(RESET_URL, params={"dryRun": "true"}).mock(return_value=_ok(True))
    real = respx.post(RESET_URL, params={"dryRun": "false"}).mock(return_value=_ok(False))

    result = runner.invoke(app, ["network", "reset"], input="y\n", env=WIDE)

    assert result.exit_code == 0, result.stdout
    assert dry.call_count == 1 and real.call_count == 1
    assert respx.calls[0].request is dry.calls.last.request  # dry run came first
    assert "Reset ALL nodes in environment 'local'?" in result.stdout
    assert "(study: 3, block: 40)" in result.stdout  # zero counts are left out
    assert [name for name, _ in org_resets] == ["globo"]


@respx.mock
def test_prompt_omits_counts_when_nothing_to_delete(tty: None, org_resets: list) -> None:
    _caller()
    respx.post(RESET_URL, params={"dryRun": "true"}).mock(
        return_value=_ok(True, {"study": 0, "block": 0})
    )
    respx.post(RESET_URL, params={"dryRun": "false"}).mock(return_value=_ok(False, {}))

    result = runner.invoke(app, ["network", "reset"], input="y\n", env=WIDE)

    assert result.exit_code == 0, result.stdout
    assert "study and block data and the local" in result.stdout
    assert "(" not in result.stdout.split("block data")[1].split("\n")[0]


@respx.mock
def test_declined_prompt_changes_nothing(tty: None, org_resets: list) -> None:
    _caller()
    _orgs("globo")
    respx.post(RESET_URL, params={"dryRun": "true"}).mock(return_value=_ok(True))
    real = respx.post(RESET_URL, params={"dryRun": "false"}).mock(return_value=_ok(False))

    result = runner.invoke(app, ["network", "reset"], input="n\n", env=WIDE)

    assert result.exit_code == 0, result.stdout
    assert not real.called
    assert org_resets == []


# --------------------------------------------------------------------------- #
# refusals: nothing is deleted anywhere
# --------------------------------------------------------------------------- #


def test_without_tty_or_yes_refuses_before_any_request(org_resets: list) -> None:
    _caller()

    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(RESET_URL)
        result = runner.invoke(app, ["network", "reset"], env=WIDE)

    assert result.exit_code == 1
    assert "pass --yes to run non-interactively" in result.stdout
    assert not route.called
    assert org_resets == []


@respx.mock
def test_409_names_each_node_with_its_cp_state(org_resets: list) -> None:
    _caller()
    _orgs("globo")
    respx.post(RESET_URL).mock(
        return_value=httpx.Response(
            409,
            json={
                "detail": "Every node must be DOWN before the network can be reset.",
                "context": {
                    "nodes": [
                        {"orgId": "org-0", "orgName": "globo", "orgNode": 1, "state": "UP"},
                        {"orgId": "org-1", "orgName": "acme", "orgNode": 2, "state": "STANDBY"},
                    ]
                },
            },
        )
    )

    result = runner.invoke(app, ["network", "reset", "--yes"], env=WIDE)

    assert result.exit_code == 1
    assert "globo/node1 (UP), acme/node2 (STANDBY)" in result.stdout
    assert org_resets == []


@respx.mock
def test_409_without_a_node_list_falls_back_to_the_server_message(org_resets: list) -> None:
    _caller()
    respx.post(RESET_URL).mock(
        return_value=httpx.Response(409, json={"detail": "Every node must be DOWN."})
    )

    result = runner.invoke(app, ["network", "reset", "--yes"], env=WIDE)

    assert result.exit_code == 1
    assert "Every node must be DOWN." in result.stdout
    assert org_resets == []


@respx.mock
@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (403, "must be a PLATFORM_ADMIN user"),
        (404, "doesn't allow network reset"),
    ],
)
def test_403_and_404_get_their_own_messages(
    status: int, expected: str, org_resets: list
) -> None:
    _caller()
    respx.post(RESET_URL).mock(return_value=httpx.Response(status, json={"detail": "x"}))

    result = runner.invoke(app, ["network", "reset", "--yes"], env=WIDE)

    assert result.exit_code == 1
    assert expected in result.stdout
    assert org_resets == []


@respx.mock
def test_refusal_on_dry_run_stops_before_the_prompt(tty: None, org_resets: list) -> None:
    _caller()
    dry = respx.post(RESET_URL, params={"dryRun": "true"}).mock(
        return_value=httpx.Response(404, json={"detail": "Not Found"})
    )
    real = respx.post(RESET_URL, params={"dryRun": "false"}).mock(return_value=_ok(False))

    result = runner.invoke(app, ["network", "reset"], input="y\n", env=WIDE)

    assert result.exit_code == 1
    assert dry.called and not real.called
    assert "Are you sure" not in result.stdout
    assert org_resets == []


@respx.mock
def test_server_error_is_reported_and_nothing_is_reset(org_resets: list) -> None:
    _caller()
    respx.post(RESET_URL).mock(
        return_value=httpx.Response(500, json={"detail": "Reset transaction rolled back."})
    )

    result = runner.invoke(app, ["network", "reset", "--yes"], env=WIDE)

    assert result.exit_code == 1
    assert "Reset transaction rolled back." in result.stdout
    assert org_resets == []


@respx.mock
def test_unreachable_cp_is_reported_and_nothing_is_reset(org_resets: list) -> None:
    _caller()
    respx.post(RESET_URL).mock(side_effect=httpx.ConnectError("refused"))

    result = runner.invoke(app, ["network", "reset", "--yes"], env=WIDE)

    assert result.exit_code == 1
    assert "could not reach" in result.stdout
    assert org_resets == []


# --------------------------------------------------------------------------- #
# start / stop: PLATFORM_ADMIN, checked first, passed down to every org call
# --------------------------------------------------------------------------- #


@pytest.fixture
def org_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, dict[str, Any]]]:
    """Record org start/stop instead of touching real nodes."""
    calls: list[tuple[str, str, dict[str, Any]]] = []
    for verb in ("start", "stop"):
        def record(org_name: str, _verb: str = verb, **kwargs: Any) -> None:
            calls.append((_verb, org_name, kwargs))
        monkeypatch.setattr(network.org, verb, record)
    return calls


@pytest.mark.parametrize(
    ("verb", "order"),
    [("start", ["globo", "acme", "zeta"]), ("stop", ["zeta", "acme", "globo"])],
)
def test_platform_admin_runs_network_start_and_stop_down_every_org(
    verb: str, order: list[str], org_calls: list
) -> None:
    _orgs("globo", "acme", "veritaslock", "zeta")
    add_user("ops", [{"orgId": "veritaslock", "role": "PLATFORM_ADMIN"}])

    result = runner.invoke(app, ["network", verb], env=WIDE)

    assert result.exit_code == 0, result.output
    assert [name for _, name, _ in org_calls] == order
    assert all(v == verb and kw == {"env": "local", "as_": "ops"} for v, _, kw in org_calls)


def test_network_start_passes_the_as_identity(org_calls: list) -> None:
    _orgs("globo")
    add_user("ops", [{"orgId": "org-0", "role": "ORG_ADMIN"}])
    add_user("boss", [{"orgId": "veritaslock", "role": "PLATFORM_ADMIN"}], default=False)

    result = runner.invoke(app, ["network", "start", "--as", "boss"], env=WIDE)

    assert result.exit_code == 0, result.output
    assert [kw["as_"] for _, _, kw in org_calls] == ["boss"]


@pytest.mark.parametrize("verb", ["start", "stop"])
@pytest.mark.parametrize("role", ["ORG_ADMIN", "USER", "KEY_READER"])
def test_network_start_and_stop_refuse_anyone_but_a_platform_admin(
    verb: str, role: str, org_calls: list
) -> None:
    _orgs("globo")
    add_user("ops", [{"orgId": "org-0", "role": role}])

    result = runner.invoke(app, ["network", verb], env=WIDE)

    assert result.exit_code == 1
    assert "'ops' needs PLATFORM_ADMIN for network commands." in result.output
    assert org_calls == []


@pytest.mark.parametrize("verb", ["start", "stop"])
@pytest.mark.parametrize("role", ["NODE", "SYSTEM"])
def test_network_start_and_stop_refuse_a_service_account(verb: str, role: str, org_calls: list) -> None:
    _orgs("globo")
    add_service_account("ops-sa", "globo", role)

    result = runner.invoke(app, ["network", verb, "--as", "ops-sa"], env=WIDE)

    assert result.exit_code == 1
    assert "service accounts can't run this command" in result.output
    assert org_calls == []


@pytest.mark.parametrize("verb", ["start", "stop"])
def test_network_start_and_stop_without_an_identity_are_refused(verb: str, org_calls: list) -> None:
    _orgs("globo")
    store.ensure_local_environment_seeded()

    result = runner.invoke(app, ["network", verb], env=WIDE)

    assert result.exit_code == 1
    assert "No identity selected" in result.output
    assert org_calls == []


# --------------------------------------------------------------------------- #
# network start: failed nodes in any org are summarized, the rest still start
# --------------------------------------------------------------------------- #

from pathlib import Path

from vl.commands import node as node_cmd
from vl.commands._shared import CliError, report_errors


def _org_with_nodes(name: str, org_id: str, count: int) -> None:
    store.ensure_local_environment_seeded()
    store.upsert_organization("local", name, org_id, name.title(), True, "2026-01-01T00:00:00Z")
    for n in range(1, count + 1):
        sa = store.add_identity("local", "SERVICE_ACCOUNT", f"sa-{name}-{n}", f"{name}-{n}", f"{name}-{n}")
        store.set_svc_acct(sa.id, "local", name, f"client-{name}-{n}", "secret", role="NODE")
        store.add_node("local", name, n, sa.id, 7000 + n)


@pytest.fixture
def node_starts(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Run network start through the real org start, with node.start replaced."""

    def install(failing: dict[tuple[str, int], str]) -> list[tuple[str, int]]:
        calls: list[tuple[str, int]] = []

        def start(org_name: str, n: int, **_: Any) -> None:
            calls.append((org_name, n))
            with report_errors():  # as the real command does
                if failing.get((org_name, n)) == "sigabrt":
                    raise node_cmd.NodeStartFailed(f"{org_name}/node{n}", -6, Path("/no/such.out"))
                if failing.get((org_name, n)) == "exit75":
                    raise node_cmd.NodeStartFailed(f"{org_name}/node{n}", 75, Path("/no/such.out"))
                print(f"Started {org_name}/node{n} (pid: 100{n}).")

        monkeypatch.setattr(network.org.node, "start", start)
        return calls

    _org_with_nodes("globo", "org-1", 2)
    _org_with_nodes("acme", "org-2", 2)
    add_user("ops", [{"orgId": "veritaslock", "role": "PLATFORM_ADMIN"}])
    return install


def test_network_start_summarizes_failed_nodes_and_starts_the_rest(node_starts: Any) -> None:
    calls = node_starts({("globo", 2): "sigabrt", ("acme", 1): "exit75"})

    result = runner.invoke(app, ["network", "start"], env=WIDE)

    assert result.exit_code == 1
    assert calls == [("globo", 1), ("globo", 2), ("acme", 1), ("acme", 2)]
    out = result.output
    assert "Started globo/node1" in out and "Started acme/node2" in out
    assert "Error: 1 of 2 nodes in globo failed to start:" in out
    assert "Error: 1 of 2 nodes in acme failed to start:" in out
    assert out.rstrip().endswith(
        "Error: 2 failed to start across the network:\n"
        "  globo/node2: exited right after starting (killed by SIGABRT)\n"
        "  acme/node1: exited right after starting (exited with code 75)"
    )


def test_network_start_without_failures_is_unchanged(node_starts: Any) -> None:
    calls = node_starts({})

    result = runner.invoke(app, ["network", "start"], env=WIDE)

    assert result.exit_code == 0, result.output
    assert len(calls) == 4
    assert "Error" not in result.output and "failed" not in result.output


def test_network_start_reports_an_org_that_fails_outright(
    node_starts: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    node_starts({})

    def broken(org_name: str, **_: Any) -> None:
        with report_errors():
            raise CliError(f"cannot read nodes for {org_name}")

    real = network.org.start
    monkeypatch.setattr(
        network.org, "start", lambda name, **kw: broken(name) if name == "acme" else real(name, **kw)
    )

    result = runner.invoke(app, ["network", "start"], env=WIDE)

    assert result.exit_code == 1
    assert "Started globo/node1" in result.output and "Started globo/node2" in result.output
    assert result.output.rstrip().endswith(
        "Error: 1 failed to start across the network:\n  acme: cannot read nodes for acme"
    )
