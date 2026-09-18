"""Tests for `vl whoami`."""

from __future__ import annotations

from typer.testing import CliRunner

from vl.app import app
from vl.lib import store

runner = CliRunner()


def test_whoami_with_no_default_identity() -> None:
    store.ensure_local_environment_seeded()
    result = runner.invoke(app, ["whoami"], env={"VL_OUTPUT": "json"})
    assert result.exit_code == 0, result.stdout
    assert "local" in result.stdout
    assert "none" in result.stdout.lower()


def test_whoami_shows_default_identity() -> None:
    store.ensure_local_environment_seeded()
    ident = store.add_identity("local", "USER", "u-1", "alice", "alice")
    store.set_user_acct(ident.id, "pw")
    store.set_default_identity("local", "alice")

    result = runner.invoke(app, ["whoami"], env={"VL_OUTPUT": "json"})

    assert result.exit_code == 0, result.stdout
    assert '"acting_as": "alice"' in result.stdout and '"kind": "USER"' in result.stdout


def test_whoami_honours_as_flag() -> None:
    store.ensure_local_environment_seeded()
    store.add_identity("local", "SERVICE_ACCOUNT", "sa-1", "sa-1", "system")

    result = runner.invoke(app, ["whoami", "--as", "system"], env={"VL_OUTPUT": "json"})

    assert result.exit_code == 0, result.stdout
    assert '"acting_as": "system"' in result.stdout and '"kind": "SERVICE_ACCOUNT"' in result.stdout


def test_whoami_shows_org_and_role_from_single_membership() -> None:
    store.ensure_local_environment_seeded()
    store.upsert_organization("local", "globo", "org-1", "Globo", active=True)
    ident = store.add_identity("local", "USER", "u-1", "alice", "alice")
    store.set_user_acct(ident.id, "pw")
    store.upsert_org_membership(ident.id, "local", "globo", "KEY_READER")
    store.set_default_identity("local", "alice")

    result = runner.invoke(app, ["whoami"], env={"VL_OUTPUT": "json"})
    assert result.exit_code == 0, result.stdout
    assert '"org": "globo"' in result.stdout
    assert '"role": "KEY_READER"' in result.stdout
    assert "idp_base_url" not in result.stdout


def test_whoami_service_account_has_no_role() -> None:
    store.ensure_local_environment_seeded()
    store.upsert_organization("local", "globo", "org-1", "Globo", active=True)
    ident = store.add_identity("local", "SERVICE_ACCOUNT", "sa-1", "sa-1", "sys")
    store.set_svc_acct(ident.id, "local", "globo", "sa-1", "s", key_version=1)
    store.set_default_identity("local", "sys")

    result = runner.invoke(app, ["whoami"], env={"VL_OUTPUT": "json"})
    assert result.exit_code == 0, result.stdout
    assert '"org": "globo"' in result.stdout
    assert '"role": "-"' in result.stdout


def test_whoami_ambiguous_org() -> None:
    store.ensure_local_environment_seeded()
    for name in ("globo", "acme"):
        store.upsert_organization("local", name, f"o-{name}", name, active=True)
    ident = store.add_identity("local", "USER", "u-1", "alice", "alice")
    store.set_user_acct(ident.id, "pw")
    store.upsert_org_membership(ident.id, "local", "globo", "USER")
    store.upsert_org_membership(ident.id, "local", "acme", "USER")
    store.set_default_identity("local", "alice")

    result = runner.invoke(app, ["whoami"], env={"VL_OUTPUT": "json"})
    assert "ambiguous" in result.stdout
