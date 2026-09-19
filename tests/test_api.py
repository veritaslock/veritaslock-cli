"""Tests for vl.lib.api — problem+json handling and login."""

from __future__ import annotations

import httpx
import pytest
import respx

from vl.lib import api

BASE = "http://idp.test"


@respx.mock
def test_get_returns_parsed_json() -> None:
    respx.get(f"{BASE}/v1/organizations", params={"name": "globo"}).mock(
        return_value=httpx.Response(200, json={"id": "org-1", "name": "globo"})
    )
    with api.AppClient(BASE) as client:
        assert client.get("/v1/organizations", params={"name": "globo"})["id"] == "org-1"


@respx.mock
def test_204_returns_none() -> None:
    respx.delete(f"{BASE}/v1/organizations/org-1/members/u-1").mock(
        return_value=httpx.Response(204)
    )
    with api.AppClient(BASE, token="t") as client:
        assert client.delete("/v1/organizations/org-1/members/u-1") is None


@respx.mock
def test_problem_json_becomes_api_error() -> None:
    respx.post(f"{BASE}/v1/organizations").mock(
        return_value=httpx.Response(
            409,
            headers={"X-Correlation-Id": "abc-123"},
            json={
                "detail": "Organization name already exists.",
                "errorCode": "CONFLICT",
                "context": {"name": "globo"},
            },
        )
    )
    with api.AppClient(BASE, token="t") as client:
        with pytest.raises(api.ApiError) as excinfo:
            client.post("/v1/organizations", json={"name": "globo"})

    err = excinfo.value
    assert err.status_code == 409
    assert err.error_code == "CONFLICT"
    assert "already exists" in err.detail
    assert err.correlation_id == "abc-123"


@respx.mock
def test_validation_fields_are_captured() -> None:
    respx.post(f"{BASE}/v1/organizations").mock(
        return_value=httpx.Response(
            400,
            json={
                "detail": "Validation failed.",
                "errorCode": "VALIDATION_FAILED",
                "context": {"invalidFields": [{"field": "displayName", "message": "blank"}]},
            },
        )
    )
    with api.AppClient(BASE, token="t") as client:
        with pytest.raises(api.ApiError) as excinfo:
            client.post("/v1/organizations", json={})
    assert excinfo.value.invalid_fields == [{"field": "displayName", "message": "blank"}]


@respx.mock
def test_transport_failure_becomes_api_error() -> None:
    respx.get(f"{BASE}/v1/organizations").mock(side_effect=httpx.ConnectError("boom"))
    with api.AppClient(BASE) as client:
        with pytest.raises(api.ApiError) as excinfo:
            client.get("/v1/organizations")
    assert excinfo.value.status_code == 0


@respx.mock
def test_login_returns_token() -> None:
    route = respx.post(f"{BASE}/auth/user/login").mock(
        return_value=httpx.Response(
            200, json={"accessToken": "jwt-abc", "tokenType": "Bearer", "expiresIn": 900}
        )
    )
    result = api.login(BASE, "alice", "hunter2")
    assert result.access_token == "jwt-abc"
    assert result.expires_in == 900
    assert route.calls.last.request.content == b'{"identifier":"alice","password":"hunter2"}'


def test_decode_jwt_payload_reads_claims() -> None:
    # header.payload.sig with payload {"sub":"u-1","typ":"user"}
    token = "eyJhbGciOiJFZERTQSJ9.eyJzdWIiOiJ1LTEiLCJ0eXAiOiJ1c2VyIn0.sig"
    assert api.decode_jwt_payload(token)["sub"] == "u-1"


@respx.mock
def test_login_bad_credentials_raises() -> None:
    respx.post(f"{BASE}/auth/user/login").mock(
        return_value=httpx.Response(401, json={"detail": "Bad credentials.", "errorCode": "UNAUTHORIZED"})
    )
    with pytest.raises(api.ApiError):
        api.login(BASE, "alice", "wrong")


@respx.mock
def test_service_account_token() -> None:
    route = respx.post(f"{BASE}/auth/service-account/token").mock(
        return_value=httpx.Response(200, json={"accessToken": "sa-jwt", "expiresIn": 600})
    )
    result = api.service_account_token(BASE, "client-1", "sekret")
    assert result.access_token == "sa-jwt"
    assert result.expires_in == 600
    assert route.calls.last.request.content == b'{"clientId":"client-1","clientSecret":"sekret"}'
