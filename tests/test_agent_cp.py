"""Startup authentication and registration (vl-agent-spec.md §2.3, §2.4) and the
heartbeat's answers (§5), against a fake control plane."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

import pytest

from _helpers import add_service_account
from vl.agent import EXIT_CONFIG, EXIT_FAILED, EXIT_NOPERM, EXIT_UNAVAILABLE, AgentExit
from vl.agent import cp
from vl.agent.handler import AgentIdentity
from vl.agent.runner import heartbeat_once
from vl.lib import api, store

LOG = logging.getLogger("test.agent.cp")


def conflict(reason: str, **context: Any) -> api.ApiError:
    return api.ApiError(409, "conflict", context={"reason": reason, **context})


class FakeCall:
    """Stands in for ``auth.authed_call``: answers each request from a script."""

    def __init__(self, *answers: Any) -> None:
        self.answers = list(answers)
        self.requests: list[tuple[str, str, Any]] = []

    def __call__(self, identity: store.Identity, idp: str, fn: Callable[[Any], Any], base: str,
                 **kw: Any) -> Any:
        assert kw.get("allow_prompt") is False
        outer = self

        class Client:
            def post(self, path: str, *, json: Any = None) -> Any:
                outer.requests.append(("POST", path, json))
                answer = outer.answers.pop(0)
                if isinstance(answer, Exception):
                    raise answer
                return answer

        return fn(Client())


@pytest.fixture
def env() -> store.Environment:
    store.ensure_local_environment_seeded()
    return store.get_environment("local")


def control(env: store.Environment, call: FakeCall, role: str = "NODE_AGENT") -> cp.ControlPlane:
    store.upsert_organization("local", "globo", "org-1", "Globo", True, "x")
    ident = add_service_account("globo-agent", "globo", role=role)
    return cp.ControlPlane(env, ident, call=call)


class Sleeps(list[float]):
    def __call__(self, s: float) -> None:
        self.append(s)


# --- authentication ----------------------------------------------------------------------


def test_the_caller_is_read_from_the_token(env: store.Environment) -> None:
    c = control(env, FakeCall())
    assert cp.authenticate(c, LOG, sleep=Sleeps()) == cp.Caller("sa-globo-agent", "org-1")


def test_a_token_that_is_not_node_agent_exits_77(env: store.Environment) -> None:
    c = control(env, FakeCall(), role="NODE")
    with pytest.raises(AgentExit) as e:
        cp.authenticate(c, LOG, sleep=Sleeps())
    assert e.value.code == EXIT_NOPERM and "NODE_AGENT" in e.value.message


def test_refused_credentials_exit_77(env: store.Environment, monkeypatch: pytest.MonkeyPatch) -> None:
    c = control(env, FakeCall())

    def refused(*a: Any, **k: Any) -> str:
        raise api.ApiError(401, "Invalid client credentials.")

    monkeypatch.setattr(cp.auth, "get_token", refused)
    with pytest.raises(AgentExit) as e:
        cp.authenticate(c, LOG, sleep=Sleeps())
    assert e.value.code == EXIT_NOPERM


def test_an_unreachable_identity_provider_is_retried_for_a_minute_then_69(
        env: store.Environment, monkeypatch: pytest.MonkeyPatch) -> None:
    c = control(env, FakeCall())
    calls = []

    def down(*a: Any, **k: Any) -> str:
        calls.append(1)
        raise api.ApiError(0, "could not reach")

    monkeypatch.setattr(cp.auth, "get_token", down)
    sleeps = Sleeps()
    with pytest.raises(AgentExit) as e:
        cp.authenticate(c, LOG, sleep=sleeps)
    assert e.value.code == EXIT_UNAVAILABLE
    assert 55 <= sum(sleeps) <= 70 and len(calls) == len(sleeps) + 1


# --- registration ------------------------------------------------------------------------


def test_register_sends_name_host_and_version(env: store.Environment) -> None:
    call = FakeCall({"agentId": "a-1", "orgId": "org-1"})
    reg = cp.register(control(env, call), "dev-box", "192.168.8.2", "0.1.0", LOG, sleep=Sleeps(),
                      stale_seconds=30)
    assert reg == cp.Registration("a-1", "org-1")
    assert call.requests == [("POST", "/v1/agents",
                              {"name": "dev-box", "host": "192.168.8.2", "agentVersion": "0.1.0"})]


def test_already_active_waits_for_staleness_and_tries_once_more(env: store.Environment) -> None:
    call = FakeCall(conflict("AGENT_ALREADY_ACTIVE", host="10.0.0.9"), {"agentId": "a-1", "orgId": "org-1"})
    sleeps = Sleeps()
    reg = cp.register(control(env, call), "n", "h", "v", LOG, sleep=sleeps, stale_seconds=30)
    assert reg.agent_id == "a-1"
    assert sleeps == [32]


def test_already_active_twice_exits_78(env: store.Environment) -> None:
    call = FakeCall(conflict("AGENT_ALREADY_ACTIVE"), conflict("AGENT_ALREADY_ACTIVE"))
    with pytest.raises(AgentExit) as e:
        cp.register(control(env, call), "n", "h", "v", LOG, sleep=Sleeps(), stale_seconds=30)
    assert e.value.code == EXIT_CONFIG and "another host" in e.value.message


@pytest.mark.parametrize("error, code, text", [
    (conflict("AGENT_HOST_TAKEN"), EXIT_CONFIG, "already registered for host"),
    (conflict("AGENT_NAME_TAKEN"), EXIT_CONFIG, "--name"),
    (api.ApiError(403, "not a NODE_AGENT"), EXIT_NOPERM, "refused"),
    (api.ApiError(401, "expired"), EXIT_NOPERM, "refused"),
    (api.ApiError(400, "bad name"), EXIT_CONFIG, "invalid"),
    (api.ApiError(404, "no such endpoint"), EXIT_FAILED, "failed"),
])
def test_each_refusal_has_its_exit_status(env: store.Environment, error: api.ApiError, code: int,
                                          text: str) -> None:
    with pytest.raises(AgentExit) as e:
        cp.register(control(env, FakeCall(error)), "n", "h", "v", LOG, sleep=Sleeps(), stale_seconds=30)
    assert e.value.code == code and text in e.value.message


def test_an_unreachable_control_plane_is_retried_then_69(env: store.Environment) -> None:
    call = FakeCall(*[api.ApiError(0, "down")] * 3, api.ApiError(503, "starting"),
                    {"agentId": "a-1", "orgId": "org-1"})
    sleeps = Sleeps()
    c = control(env, call)
    assert cp.register(c, "n", "h", "v", LOG, sleep=sleeps, stale_seconds=30).agent_id == "a-1"
    assert sleeps == [1, 2, 4, 8]
    call.answers = [api.ApiError(0, "down")] * 20
    with pytest.raises(AgentExit) as e:
        cp.register(c, "n", "h", "v", LOG, sleep=Sleeps(), stale_seconds=30)
    assert e.value.code == EXIT_UNAVAILABLE


# --- heartbeat ---------------------------------------------------------------------------


def me() -> AgentIdentity:
    return AgentIdentity(agent_id="a-1", name="dev-box", host="h", org_id="org-1", org_name="globo")


def test_heartbeat_204_carries_on(env: store.Environment) -> None:
    call = FakeCall(None)
    identity = me()
    heartbeat_once(control(env, call), identity, "2026-11-13T12:00:00.000Z", LOG)
    assert call.requests == [("POST", "/v1/agents/a-1/heartbeat",
                              {"agentVersion": "0.1.0", "startedAt": "2026-11-13T12:00:00.000Z"})]


def test_heartbeat_404_registers_again(env: store.Environment) -> None:
    call = FakeCall(api.ApiError(404, "Agent not found."), {"agentId": "a-2", "orgId": "org-1"})
    identity = me()
    heartbeat_once(control(env, call), identity, "t", LOG)
    assert call.requests[1] == ("POST", "/v1/agents", {"name": "dev-box", "host": "h", "agentVersion": "0.1.0"})
    assert identity.agent_id == "a-2"


def test_heartbeat_404_and_a_failed_registration_keep_going(env: store.Environment) -> None:
    call = FakeCall(api.ApiError(404, "Agent not found."), api.ApiError(0, "down"))
    identity = me()
    heartbeat_once(control(env, call), identity, "t", LOG)
    assert identity.agent_id == "a-1"


def test_heartbeat_id_mismatch_exits_78(env: store.Environment) -> None:
    with pytest.raises(AgentExit) as e:
        heartbeat_once(control(env, FakeCall(conflict("AGENT_ID_MISMATCH"))), me(), "t", LOG)
    assert e.value.code == EXIT_CONFIG


@pytest.mark.parametrize("error", [api.ApiError(0, "down"), api.ApiError(502, "bad gateway"),
                                   api.ApiError(401, "refused"), conflict("SOMETHING_ELSE")])
def test_heartbeat_other_answers_are_logged_and_never_stop_the_agent(
        env: store.Environment, error: api.ApiError, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="test.agent.cp"):
        heartbeat_once(control(env, FakeCall(error)), me(), "t", LOG)
    assert caplog.records and caplog.records[0].levelno >= logging.WARNING
