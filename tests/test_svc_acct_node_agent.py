"""The NODE_AGENT service-account role (vl-agent-cli-spec.md §10).

`vl svc-acct add --role NODE_AGENT` creates it in an org (the identity provider
decides who may: an ORG_ADMIN of the org, or a SYSTEM account), and
`vl svc-acct list --role NODE_AGENT` filters on it at the server.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
import respx
from typer.testing import CliRunner

from vl.app import app
from vl.lib import store
from vl.lib.roles import ServiceAccountRole

runner = CliRunner()
IDP = "http://localhost:8080"
ORG_DTO = {"id": "org-1", "name": "globo", "displayName": "Globo", "active": True, "createdAt": "2026-01-01T00:00:00Z"}
WIDE = {"COLUMNS": "200"}


def _caller() -> None:
    store.ensure_local_environment_seeded()
    ident = store.add_identity("local", "USER", "u-root", "root", "root")
    store.set_user_acct(ident.id, "pw")
    store.set_default_identity("local", "root")
    now = datetime.now(timezone.utc)
    store.set_cached_token(ident.id, "caller-jwt", now, now + timedelta(hours=1))


def _created() -> dict[str, object]:
    return {"id": "sa-agent", "orgId": "org-1", "displayName": "globo-agent", "role": "NODE_AGENT",
            "status": "PENDING", "keyVersion": 1, "bootstrapHash": "boot-1", "publicKey": None}


def test_node_agent_is_a_role_choice() -> None:
    assert ServiceAccountRole("NODE_AGENT") is ServiceAccountRole.NODE_AGENT
    for command in (["svc-acct", "add", "--help"], ["svc-acct", "list", "--help"], ["svc-acct", "update", "--help"]):
        result = runner.invoke(app, command, env=WIDE)
        assert result.exit_code == 0 and "NODE_AGENT" in result.output, command


@respx.mock
def test_add_role_node_agent_creates_it_in_the_org_and_caches_the_role() -> None:
    _caller()
    respx.get(f"{IDP}/v1/organizations", params={"name": "globo"}).mock(
        return_value=httpx.Response(200, json=ORG_DTO)
    )
    create = respx.post(f"{IDP}/v1/service-accounts").mock(return_value=httpx.Response(201, json=_created()))
    respx.patch(f"{IDP}/v1/service-accounts/sa-agent/public-key").mock(
        return_value=httpx.Response(200, json={**_created(), "status": "ACTIVE"})
    )

    result = runner.invoke(app, ["svc-acct", "add", "globo-agent", "--role", "NODE_AGENT", "--org", "globo"], env=WIDE)

    assert result.exit_code == 0, result.output
    body = json.loads(create.calls.last.request.content)
    assert body["role"] == "NODE_AGENT" and body["orgId"] == "org-1"
    cred = store.get_svc_acct(store.get_identity("local", "globo-agent").id)
    assert cred is not None and cred.role == "NODE_AGENT" and cred.org_name == "globo"


@respx.mock
def test_add_role_node_agent_needs_an_org() -> None:
    _caller()  # a caller with no single default org

    result = runner.invoke(app, ["svc-acct", "add", "globo-agent", "--role", "NODE_AGENT"], env=WIDE)

    assert result.exit_code == 1
    assert "no --org given" in result.output
    assert len(respx.calls) == 0


@respx.mock
def test_add_role_node_agent_refused_by_the_identity_provider_is_its_error() -> None:
    _caller()
    respx.get(f"{IDP}/v1/organizations", params={"name": "globo"}).mock(
        return_value=httpx.Response(200, json=ORG_DTO)
    )
    respx.post(f"{IDP}/v1/service-accounts").mock(return_value=httpx.Response(
        403, json={"status": 403, "title": "Forbidden", "detail": "Requires ORG_ADMIN of org org-1 or SYSTEM"}
    ))

    result = runner.invoke(app, ["svc-acct", "add", "globo-agent", "--role", "NODE_AGENT", "--org", "globo"], env=WIDE)

    assert result.exit_code == 1
    assert "Requires ORG_ADMIN of org org-1 or SYSTEM" in result.output
    assert not [i for i in store.list_identities("local", kind="SERVICE_ACCOUNT")]


@respx.mock
def test_list_role_node_agent_filters_at_the_server() -> None:
    _caller()
    route = respx.get(f"{IDP}/v1/service-accounts").mock(return_value=httpx.Response(200, json={"items": [
        {"id": "sa-agent", "displayName": "globo-agent", "role": "NODE_AGENT", "status": "ACTIVE", "keyVersion": 1},
    ]}))

    result = runner.invoke(app, ["svc-acct", "list", "--remote", "--role", "NODE_AGENT"], env=WIDE)

    assert result.exit_code == 0, result.output
    assert route.calls.last.request.url.params["role"] == "NODE_AGENT"
    assert "NODE_AGENT" in result.output and "globo-agent" in result.output


def test_list_local_shows_a_cached_node_agent_role() -> None:
    store.ensure_local_environment_seeded()
    store.upsert_organization("local", "globo", "org-1", "Globo", True, "t0")
    ident = store.add_identity("local", "SERVICE_ACCOUNT", "sa-agent", "sa-agent", "globo-agent")
    store.set_svc_acct(ident.id, "local", "globo", "sa-agent", "s", role="NODE_AGENT")

    result = runner.invoke(app, ["svc-acct", "list"], env={**WIDE, "VL_OUTPUT": "json"})

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)[0]["role"] == "NODE_AGENT"
