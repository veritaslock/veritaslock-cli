"""Thin HTTP client for the VeritasLock IdP API.

Wraps `httpx` with two `vl`-specific concerns:

* RFC 7807 ``application/problem+json`` errors are turned into a single
  ``ApiError`` carrying the server's ``detail`` / ``errorCode`` / field errors /
  correlation id, so command code can render one clean line.
* connection failures become an ``ApiError`` too, rather than a raw traceback.
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from types import TracebackType
from typing import Any

import httpx

DEFAULT_TIMEOUT = 10.0


class ApiError(Exception):
    """A failed API call — an HTTP error response or a transport failure."""

    def __init__(
        self,
        status_code: int,
        detail: str,
        *,
        error_code: str | None = None,
        invalid_fields: list[Any] | None = None,
        correlation_id: str | None = None,
    ) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.error_code = error_code
        self.invalid_fields = invalid_fields or []
        self.correlation_id = correlation_id

    def __str__(self) -> str:
        if self.status_code:
            return f"{self.detail} (HTTP {self.status_code})"
        return self.detail


def _raise_for_problem(resp: httpx.Response) -> None:
    if resp.is_success:
        return

    correlation_id = resp.headers.get("X-Correlation-Id")
    detail = f"request failed with HTTP {resp.status_code}"
    error_code: str | None = None
    invalid_fields: list[Any] = []

    try:
        body = resp.json()
    except ValueError:
        body = None

    if isinstance(body, dict):
        detail = body.get("detail") or body.get("title") or detail
        error_code = body.get("errorCode")
        context = body.get("context")
        if isinstance(context, dict):
            fields = context.get("invalidFields")
            if isinstance(fields, list):
                invalid_fields = fields

    raise ApiError(
        resp.status_code,
        detail,
        error_code=error_code,
        invalid_fields=invalid_fields,
        correlation_id=correlation_id,
    )


class IdpClient:
    """A short-lived client bound to one environment's IdP base URL."""

    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            headers=headers,
            transport=transport,
        )

    def __enter__(self) -> IdpClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._client.close()

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any | None = None,
    ) -> Any:
        try:
            resp = self._client.request(method, path, params=params, json=json)
        except httpx.RequestError as exc:
            raise ApiError(
                0, f"could not reach {self._client.base_url}: {exc}"
            ) from exc
        _raise_for_problem(resp)
        if resp.status_code == httpx.codes.NO_CONTENT or not resp.content:
            return None
        return resp.json()

    def get(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        return self.request("GET", path, params=params)

    def post(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any | None = None,
    ) -> Any:
        return self.request("POST", path, params=params, json=json)

    def patch(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any | None = None,
    ) -> Any:
        return self.request("PATCH", path, params=params, json=json)

    def delete(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        return self.request("DELETE", path, params=params)


@dataclass(frozen=True)
class TokenResult:
    """The result of a successful login: the JWT and its lifetime in seconds."""

    access_token: str
    expires_in: int


def login(
    base_url: str,
    username: str,
    password: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    transport: httpx.BaseTransport | None = None,
) -> TokenResult:
    """Exchange user credentials for an access token via ``POST /auth/user/login``.

    The token is returned to the caller — persistence (if any) is the caller's call.
    """
    with httpx.Client(
        base_url=base_url.rstrip("/"), timeout=timeout, transport=transport
    ) as client:
        try:
            resp = client.post(
                "/auth/user/login",
                json={"identifier": username, "password": password},
            )
        except httpx.RequestError as exc:
            raise ApiError(0, f"could not reach {base_url}: {exc}") from exc

    _raise_for_problem(resp)
    return _token_result(resp)


def service_account_token(
    base_url: str,
    client_id: str,
    client_secret: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    transport: httpx.BaseTransport | None = None,
) -> TokenResult:
    """Exchange a client id/secret for a token via ``POST /auth/service-account/token``."""
    with httpx.Client(
        base_url=base_url.rstrip("/"), timeout=timeout, transport=transport
    ) as client:
        try:
            resp = client.post(
                "/auth/service-account/token",
                json={"clientId": client_id, "clientSecret": client_secret},
            )
        except httpx.RequestError as exc:
            raise ApiError(0, f"could not reach {base_url}: {exc}") from exc

    _raise_for_problem(resp)
    return _token_result(resp)


def _token_result(resp: httpx.Response) -> TokenResult:
    body = resp.json()
    token = body.get("accessToken")
    if not token:
        raise ApiError(resp.status_code, "auth response did not include a token")
    return TokenResult(str(token), int(body.get("expiresIn") or 0))


def decode_jwt_payload(token: str) -> dict[str, Any]:
    """Read a JWT's claims *without verifying the signature*.

    `vl` only needs claims like ``sub`` (the server-assigned id) from a token the
    server just issued to it — it never makes a trust decision on the contents.
    """
    try:
        payload_b64 = token.split(".")[1]
        padded = payload_b64 + "=" * (-len(payload_b64) % 4)
        claims = json.loads(base64.urlsafe_b64decode(padded))
    except (IndexError, ValueError, binascii.Error) as exc:
        raise ApiError(0, f"could not decode token payload: {exc}") from exc
    if not isinstance(claims, dict):
        raise ApiError(0, "token payload was not a JSON object")
    return claims
