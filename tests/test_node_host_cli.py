"""The configured node host: `vl env add|update --node-host`, `--clear-node-host`,
shown by `vl env show|list`, rendered by `vl node start` as ``node.host``. Without
one, everything is as before."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from vl.app import app
from vl.lib import nodeops, store

runner = CliRunner()
JSON = {"VL_OUTPUT": "json"}
ADD = ["env", "add", "lab", "--idp-url", "http://i", "--cp-url", "http://c", "--di-url", "http://d",
       "--token-url", "http://t", "--schema-reg-url", "http://s", "--kafka", "k:9092"]


def _show(name: str = "lab") -> dict[str, Any]:
    result = runner.invoke(app, ["env", "show", name], env=JSON)
    assert result.exit_code == 0, result.output
    shown: dict[str, Any] = json.loads(result.stdout)
    return shown


# --- vl env --------------------------------------------------------------------------


def test_add_with_a_node_host_shows_it() -> None:
    result = runner.invoke(app, [*ADD, "--node-host", " 192.168.8.2 "])
    assert result.exit_code == 0, result.output
    assert _show()["node_host"] == "192.168.8.2"  # trimmed
    listed = json.loads(runner.invoke(app, ["env", "list"], env=JSON).stdout)
    assert {e["name"]: e["node_host"] for e in listed} == {"lab": "192.168.8.2", "local": None}


def test_without_a_node_host_it_is_null() -> None:
    assert runner.invoke(app, ADD).exit_code == 0
    assert _show()["node_host"] is None
    assert store.get_environment("lab").node_host is None


def test_update_sets_changes_and_clears_it() -> None:
    assert runner.invoke(app, ADD).exit_code == 0
    assert runner.invoke(app, ["env", "update", "lab", "--node-host", "10.0.0.5"]).exit_code == 0
    assert _show()["node_host"] == "10.0.0.5"
    assert runner.invoke(app, ["env", "update", "lab", "--kafka", "k2:9092"]).exit_code == 0
    assert _show()["node_host"] == "10.0.0.5"  # other updates leave it alone
    assert runner.invoke(app, ["env", "update", "lab", "--clear-node-host"]).exit_code == 0
    assert _show()["node_host"] is None
    assert _show()["kafka_bootstrap"] == "k2:9092"


def test_set_and_clear_together_is_a_usage_error() -> None:
    assert runner.invoke(app, ADD).exit_code == 0
    result = runner.invoke(app, ["env", "update", "lab", "--node-host", "h", "--clear-node-host"])
    assert result.exit_code == 2
    assert _show()["node_host"] is None


@pytest.mark.parametrize("bad", ["", "   ", "host name", "h\nkafka.broker=evil:9092", "a=b", "x" * 254])
def test_a_value_that_is_not_an_address_is_refused(bad: str) -> None:
    result = runner.invoke(app, [*ADD, "--node-host", bad])
    assert result.exit_code == 2, result.output
    with pytest.raises(store.EnvironmentNotFoundError):
        store.get_environment("lab")


@pytest.mark.parametrize("good", ["192.168.8.2", "fe80::1", "dev-box.lan", "node_7"])
def test_addresses_and_names_are_accepted(good: str) -> None:
    assert runner.invoke(app, [*ADD, "--node-host", good]).exit_code == 0
    assert _show()["node_host"] == good


def test_update_with_only_a_token_or_registry_url_is_not_nothing() -> None:
    assert runner.invoke(app, ADD).exit_code == 0
    assert runner.invoke(app, ["env", "update", "lab", "--token-url", "http://t2"]).exit_code == 0
    assert store.get_environment("lab").token_url == "http://t2"
    nothing = runner.invoke(app, ["env", "update", "lab"])
    assert nothing.exit_code == 1 and "--clear-node-host" in nothing.output


# --- rendered into the node's configuration -------------------------------------------


def _node() -> store.Node:
    store.ensure_local_environment_seeded()
    store.upsert_organization("local", "globo", "org-1", "Globo", True, "2026-01-01T00:00:00Z")
    ident = store.add_identity("local", "SERVICE_ACCOUNT", "sa-globo-1", "globo-1", "globo-1")
    store.set_svc_acct(ident.id, "local", "globo", "client-globo-1", "secret", role="NODE",
                       private_key_path="/k/private.key", public_key_path="/k/public.key")
    return store.add_node("local", "globo", 1, ident.id, 7001)


def node_config(text: str) -> dict[str, str]:
    """``veritas-lock.conf`` read as the node reads it (Config::loadConfigFile): lines
    trimmed, blank and ``#``/``;`` lines skipped, split at the first ``=``, last wins."""
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line[0] in "#;" or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


EXPECTED = {
    "kafka.broker": "localhost:9092",
    "schema.registry.url": "http://localhost:8081",
    "veritas.data.dir": "data",
    "token.url": "http://localhost:8080/auth/service-account/authenticate",
    "control.plane.base.url": "http://localhost:8082",
    "identity.provider.base.url": "http://localhost:8080",
    "http.port": "7001",
    "org.node": "1",
}


def test_without_a_node_host_the_configuration_is_as_before(tmp_path: Path) -> None:
    node = _node()
    nodeops.generate_veritaslock_config(store.get_environment("local"), tmp_path, node)
    text = (tmp_path / "veritas-lock.conf").read_text()
    assert node_config(text) == EXPECTED
    assert "node.host" not in text


def test_with_a_node_host_the_configuration_has_node_host(tmp_path: Path) -> None:
    node = _node()
    store.update_environment("local", node_host="192.168.8.2")
    nodeops.generate_veritaslock_config(store.get_environment("local"), tmp_path, node)
    text = (tmp_path / "veritas-lock.conf").read_text()
    assert node_config(text) == {**EXPECTED, "node.host": "192.168.8.2"}
    assert text.endswith("org.node=1\nnode.host=192.168.8.2\n")  # its own line, not run on


def test_vl_node_start_renders_the_environments_node_host(isolated_home: Path,
                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    node = _node()
    store.update_environment("local", node_host="10.1.2.3")
    install = isolated_home / ".veritaslock"
    for d in ("bin", "etc", "lib"):
        (install / d).mkdir(parents=True)
    (install / "bin" / "verilock").write_text("#!/bin/sh\n")
    (install / "etc" / "log4cplus.properties").write_text("")
    (install / "etc" / "verilock-kafka-consumer.conf").write_text("")
    (install / "lib" / "liblog4cplus.so.3").write_text("")
    root = nodeops.node_root("globo", 1)
    for d in ("bin", "etc", "lib", "identity", "data"):
        (root / d).mkdir(parents=True)
    spawned: list[Path] = []
    monkeypatch.setattr(nodeops, "spawn_node", lambda n, r, emit: spawned.append(r) or nodeops.NodeResult(
        nodeops.Outcome.STARTED, "pid 1", pid=1))

    result = nodeops.start_node(store.get_environment("local"), "globo", 1, lambda t: None)

    assert result.outcome is nodeops.Outcome.STARTED and spawned == [root]
    assert node_config((root / "etc" / "veritas-lock.conf").read_text())["node.host"] == "10.1.2.3"
    assert node.port == 7001
