"""The agent's control plane and identity provider calls (vl-agent-spec.md §2.1,
§2.3, §5; cp-agent-commands-spec.md §5.3 as built).

Authentication is `vl`'s own: the service account's stored client secret, the
token cache in ``store.db``, and one re-authentication on a ``401``
(``vl.lib.auth``). The agent needs no authentication code of its own.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from vl.agent import EXIT_CONFIG, EXIT_FAILED, EXIT_NOPERM, EXIT_UNAVAILABLE, AgentExit
from vl.lib import api, auth, store

NODE_AGENT = "NODE_AGENT"
# About a minute of retries while the control plane or the identity provider is unreachable.
RETRY_DELAYS: tuple[float, ...] = (1, 2, 4, 8, 15, 15, 15)

Sleep = Callable[[float], None]


def _reason(exc: api.ApiError) -> str | None:
    """The control plane's ``context.reason`` (``AGENT_ALREADY_ACTIVE``, …), if any."""
    reason = exc.context.get("reason")
    return reason if isinstance(reason, str) else None


def unreachable(exc: api.ApiError) -> bool:
    """A transport failure, or the server is not answering properly (5xx)."""
    return exc.status_code == 0 or exc.status_code >= 500


@dataclass(frozen=True)
class Caller:
    """Who the token says the agent is."""

    service_account_id: str
    org_id: str


@dataclass(frozen=True)
class Registration:
    agent_id: str
    org_id: str


class ControlPlane:
    """The agent's REST calls, as its own ``NODE_AGENT`` service account."""

    def __init__(
        self,
        environment: store.Environment,
        identity: store.Identity,
        *,
        call: Callable[..., Any] = auth.authed_call,
    ) -> None:
        self._env = environment
        self._identity = identity
        self._call = call

    def _authed(self, fn: Callable[[api.AppClient], Any]) -> Any:
        return self._call(
            self._identity, self._env.idp_base_url, fn, self._env.cp_base_url, allow_prompt=False
        )

    def caller(self) -> Caller:
        """Authenticate (or reuse the cached token) and read who the token names."""
        token = auth.get_token(self._identity, self._env.idp_base_url, allow_prompt=False)
        claims = api.decode_jwt_payload(token)
        role, org, sub = claims.get("role"), claims.get("org"), claims.get("sub")
        if claims.get("typ") != "service-account" or role != NODE_AGENT:
            raise AgentExit(
                EXIT_NOPERM,
                f"identity {self._identity.label!r} is not a NODE_AGENT service account "
                f"(its token says role {role!r}). Create one with "
                f"`vl svc-acct add <name> --role NODE_AGENT --org <org>`.",
            )
        if not isinstance(org, str) or not org or not isinstance(sub, str) or not sub:
            raise AgentExit(EXIT_NOPERM, f"the token of {self._identity.label!r} names no org or no account")
        return Caller(service_account_id=sub, org_id=org)

    def register(self, name: str, host: str, version: str) -> Registration:
        """``POST /v1/agents``: find-or-create keyed by the token's service account."""
        body = self._authed(
            lambda c: c.post("/v1/agents", json={"name": name, "host": host, "agentVersion": version})
        )
        if not isinstance(body, dict) or not body.get("agentId") or not body.get("orgId"):
            raise api.ApiError(200, f"registration answered without agentId and orgId: {body!r}")
        return Registration(agent_id=str(body["agentId"]), org_id=str(body["orgId"]))

    def heartbeat(self, agent_id: str, version: str, started_at: str) -> None:
        """``POST /v1/agents/{agentId}/heartbeat``; ``204`` returns, anything else raises."""
        self._authed(
            lambda c: c.post(
                f"/v1/agents/{agent_id}/heartbeat",
                json={"agentVersion": version, "startedAt": started_at},
            )
        )


def with_retries(
    what: str,
    fn: Callable[[], Any],
    log: logging.Logger,
    *,
    sleep: Sleep,
    delays: Sequence[float] = RETRY_DELAYS,
) -> Any:
    """``fn()``, retried while the far end is unreachable; then exit 69."""
    for attempt, delay in enumerate([*delays, None]):
        try:
            return fn()
        except api.ApiError as exc:
            if not unreachable(exc):
                raise
            if delay is None:
                raise AgentExit(
                    EXIT_UNAVAILABLE, f"{what}: still unreachable after {attempt} retries: {exc}"
                ) from exc
            log.warning("%s: unreachable (%s); retrying in %g s", what, exc, delay)
            sleep(delay)
    raise AssertionError("unreachable")  # pragma: no cover


def authenticate(cp: ControlPlane, log: logging.Logger, *, sleep: Sleep) -> Caller:
    """Startup step 2. Refused credentials exit 77; missing ones 78; no answer 69."""
    try:
        caller: Caller = with_retries("identity provider", cp.caller, log, sleep=sleep)
        return caller
    except auth.AuthError as exc:
        raise AgentExit(EXIT_CONFIG, str(exc)) from exc
    except api.ApiError as exc:
        if exc.status_code in (400, 401, 403):
            raise AgentExit(EXIT_NOPERM, f"authentication refused: {exc}") from exc
        raise AgentExit(EXIT_FAILED, f"authentication failed: {exc}") from exc


def register(
    cp: ControlPlane,
    name: str,
    host: str,
    version: str,
    log: logging.Logger,
    *,
    sleep: Sleep,
    stale_seconds: float,
) -> Registration:
    """Startup step 3, with every refusal turned into its exit status.

    ``AGENT_ALREADY_ACTIVE`` (this account's heartbeat is fresh on another host)
    is retried once, after the control plane's staleness window: an agent that
    exited 70 because its host changed is refused until its old heartbeat goes
    stale (vl-agent-spec.md §8 item 7). A second refusal means it really is
    running elsewhere, or its credentials were copied: exit 78.
    """
    waited = False
    while True:
        try:
            registration: Registration = with_retries(
                "control plane", lambda: cp.register(name, host, version), log, sleep=sleep
            )
            return registration
        except auth.AuthError as exc:
            raise AgentExit(EXIT_CONFIG, str(exc)) from exc
        except api.ApiError as exc:
            reason = _reason(exc)
            if exc.status_code == 409 and reason == "AGENT_ALREADY_ACTIVE" and not waited:
                wait = stale_seconds + 2
                log.warning(
                    "registration refused: this agent's account is active on another host "
                    "(%s, last seen %s); waiting %g s for that heartbeat to go stale, then trying once more",
                    exc.context.get("host"), exc.context.get("lastSeenAt"), wait,
                )
                sleep(wait)
                waited = True
                continue
            if exc.status_code == 409 and reason == "AGENT_ALREADY_ACTIVE":
                raise AgentExit(
                    EXIT_CONFIG,
                    f"this agent's service account is active on another host ({exc.context.get('host')}): "
                    f"the agent is still running there, or its credentials were copied to this machine",
                ) from exc
            if exc.status_code == 409 and reason == "AGENT_HOST_TAKEN":
                raise AgentExit(
                    EXIT_CONFIG,
                    f"another agent of this org is already registered for host {host!r}; "
                    f"one agent per org and host",
                ) from exc
            if exc.status_code == 409 and reason == "AGENT_NAME_TAKEN":
                raise AgentExit(
                    EXIT_CONFIG, f"another agent of this org is already named {name!r}; pass --name"
                ) from exc
            if exc.status_code in (401, 403):
                raise AgentExit(EXIT_NOPERM, f"registration refused: {exc}") from exc
            if exc.status_code == 400:
                raise AgentExit(EXIT_CONFIG, f"registration refused as invalid: {exc}") from exc
            raise AgentExit(EXIT_FAILED, f"registration failed: {exc}") from exc
