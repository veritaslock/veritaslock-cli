"""Tests for the `vl study` command group (vl-study-spec.md §1-§3).

The control plane is stubbed with respx: every route a test doesn't register is
refused (``assert_all_mocked``), so "nothing sent" is checked by counting the calls
the router saw.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import respx
from typer.testing import CliRunner

from _helpers import add_user
from vl.app import app
from vl.commands import study as study_cmd
from vl.lib import store

runner = CliRunner()
IDP = "http://localhost:8080"
CP = "http://localhost:8082"
WIDE = {"COLUMNS": "200"}  # keep Rich from wrapping long lines
STUDY_ID = "s-1"
EVENT_ID = "ev-1"
LOOKUP = {"studyId": STUDY_ID, "includeDeleted": "true"}


def _dto(**overrides: Any) -> dict[str, Any]:
    """A StudyDto as GET /v1/studies returns it."""
    dto: dict[str, Any] = {
        "eventId": EVENT_ID,
        "studyName": "Flu 2026",
        "studyDescription": "Seasonal flu",
        "studyId": STUDY_ID,
        "studyState": "DISABLED",
        "rejectedReason": None,
        "organization": "globo",
        "startingTime": "2026-11-01T00:00:00Z",
        "endingTime": "2026-12-01T00:00:00Z",
        "clientId": "client-1",
        "createdAt": "2026-10-01T00:00:00Z",
        "createdBy": "u-ops",
        "allowMultipleEventsPerClient": False,
        "deleted": False,
    }
    dto.update(overrides)
    return dto


def _page(*items: dict[str, Any], next_cursor: str | None = None) -> httpx.Response:
    return httpx.Response(200, json={"items": list(items), "nextCursor": next_cursor})


def _problem(status: int, detail: str, **context: Any) -> httpx.Response:
    body: dict[str, Any] = {"detail": detail}
    if context:
        body["context"] = context
    return httpx.Response(status, json=body)


def _query(request: httpx.Request) -> dict[str, str]:
    return dict(request.url.params)


def _body(request: httpx.Request) -> Any:
    return json.loads(request.content)


def _token(ident: store.Identity) -> str:
    cached = store.get_cached_token(ident.id)
    assert cached is not None
    return cached.token


@pytest.fixture
def cp() -> Iterator[respx.MockRouter]:
    """A router for the IdP and control plane; unregistered requests fail."""
    with respx.mock(assert_all_called=False) as router:
        yield router


@pytest.fixture
def ops() -> store.Identity:
    """The default identity: an ORG_ADMIN of globo."""
    return add_user("ops", orgs=[{"orgId": "org-1", "role": "ORG_ADMIN"}])


@pytest.fixture
def tty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make study.py believe stdin is a terminal; the prompt still reads the runner's input."""
    monkeypatch.setattr(
        study_cmd, "sys", SimpleNamespace(stdin=SimpleNamespace(isatty=lambda: True))
    )


def _lookup(cp: respx.MockRouter, *items: dict[str, Any]) -> respx.Route:
    return cp.get(f"{CP}/v1/studies", params=LOOKUP).mock(return_value=_page(*items))


def _patch(cp: respx.MockRouter, response: httpx.Response | None = None) -> respx.Route:
    return cp.patch(f"{CP}/v1/studies/{EVENT_ID}").mock(
        return_value=response or httpx.Response(200, json=_dto())
    )


def _invoke(*args: str, input: str | None = None, env: dict[str, str] | None = None) -> Any:
    return runner.invoke(app, ["study", *args], input=input, env={**WIDE, **(env or {})})


def _assert_lookup(request: httpx.Request) -> None:
    assert request.method == "GET"
    assert request.url.path == "/v1/studies"
    assert _query(request) == LOOKUP


# --------------------------------------------------------------------------- #
# delete (§2.5)
# --------------------------------------------------------------------------- #


def test_delete_already_deleted_is_a_no_op_without_a_prompt(cp: respx.MockRouter, ops: store.Identity) -> None:
    # No --yes and no terminal: had the code reached the prompt, it would have failed.
    _lookup(cp, _dto(deleted=True))
    patch = _patch(cp)

    result = _invoke("delete", "globo", STUDY_ID)

    assert result.exit_code == 0, result.output
    assert "Study globo/s-1 is already deleted" in result.output
    assert "Are you sure" not in result.output
    assert not patch.called
    assert len(cp.calls) == 1


@pytest.mark.parametrize("state", ["DISABLED", "REJECTED"])
def test_delete_with_yes_sends_one_soft_delete_patch(
    cp: respx.MockRouter, ops: store.Identity, state: str
) -> None:
    # REJECTED deletes directly: vl sends the same PATCH, with no disable step.
    _lookup(cp, _dto(studyState=state))
    patch = _patch(cp, httpx.Response(200, json=_dto(studyState=state, deleted=True)))

    result = _invoke("delete", "globo", STUDY_ID, "--yes")

    assert result.exit_code == 0, result.output
    assert "Study globo/s-1 was deleted." in result.output
    assert patch.call_count == 1
    request = patch.calls.last.request
    assert _query(request) == {"studyId": STUDY_ID}
    assert _body(request) == {"deleted": True}
    assert request.headers["Authorization"] == f"Bearer {_token(ops)}"


def test_delete_looks_up_once_with_include_deleted_before_the_patch(
    cp: respx.MockRouter, ops: store.Identity
) -> None:
    _lookup(cp, _dto())
    _patch(cp)

    result = _invoke("delete", "globo", STUDY_ID, "--yes")

    assert result.exit_code == 0, result.output
    assert [call.request.method for call in cp.calls] == ["GET", "PATCH"]
    _assert_lookup(cp.calls[0].request)


@pytest.mark.parametrize("answer", ["y", "Y"])
def test_delete_prompt_y_sends_the_patch(
    cp: respx.MockRouter, ops: store.Identity, tty: None, answer: str
) -> None:
    _lookup(cp, _dto())
    patch = _patch(cp)

    result = _invoke("delete", "globo", STUDY_ID, input=f"{answer}\n")

    assert result.exit_code == 0, result.output
    assert (
        "Delete globo/s-1? It will be hidden from listings and can no longer be "
        "modified or restored. Are you sure?"
    ) in result.output
    assert patch.call_count == 1
    assert "was deleted" in result.output


@pytest.mark.parametrize("answer", ["n", "N", "no", ""])
def test_delete_prompt_declined_sends_nothing(
    cp: respx.MockRouter, ops: store.Identity, tty: None, answer: str
) -> None:
    _lookup(cp, _dto())
    patch = _patch(cp)

    result = _invoke("delete", "globo", STUDY_ID, input=f"{answer}\n")

    assert result.exit_code == 0, result.output
    assert "Are you sure?" in result.output
    assert not patch.called
    assert "was deleted" not in result.output


def test_delete_prompt_reasks_on_other_input_and_never_sends(
    cp: respx.MockRouter, ops: store.Identity, tty: None
) -> None:
    # click's confirm re-asks on anything it doesn't recognize; here it then runs
    # out of input and aborts.
    _lookup(cp, _dto())
    patch = _patch(cp)

    result = _invoke("delete", "globo", STUDY_ID, input="maybe\n")

    assert result.exit_code != 0
    assert not patch.called


def test_delete_without_yes_and_without_a_terminal_refuses(cp: respx.MockRouter, ops: store.Identity) -> None:
    _lookup(cp, _dto())
    patch = _patch(cp)

    result = _invoke("delete", "globo", STUDY_ID)

    assert result.exit_code == 1
    assert "vl study delete requires confirmation; pass --yes" in result.output
    assert not patch.called


@pytest.mark.parametrize(
    ("state", "detail"),
    [
        ("ACTIVE", "Only a DISABLED or REJECTED study can be deleted."),
        ("ACCEPTED", "Only a DISABLED or REJECTED study can be deleted."),
        ("FINALIZED", "A FINALIZED study can't be deleted."),
        ("ARCHIVED", "An ARCHIVED study can't be deleted."),
    ],
)
def test_delete_leaves_the_state_check_to_the_control_plane(
    cp: respx.MockRouter, ops: store.Identity, state: str, detail: str
) -> None:
    _lookup(cp, _dto(studyState=state))
    patch = _patch(cp, _problem(400, detail))

    result = _invoke("delete", "globo", STUDY_ID, "--yes")

    assert result.exit_code == 1
    assert patch.call_count == 1
    assert f"Error: {detail} (HTTP 400)" in result.output
    assert "was deleted" not in result.output


def test_delete_refuses_a_study_in_another_org(cp: respx.MockRouter, ops: store.Identity) -> None:
    _lookup(cp, _dto(organization="acme"))
    patch = _patch(cp)

    result = _invoke("delete", "globo", STUDY_ID, "--yes")

    assert result.exit_code == 1
    assert "study s-1 belongs to org acme, not globo." in result.output
    assert "not found" not in result.output
    assert not patch.called


def test_delete_of_an_unknown_study_is_not_found(cp: respx.MockRouter, ops: store.Identity) -> None:
    _lookup(cp)
    patch = _patch(cp)

    result = _invoke("delete", "globo", STUDY_ID, "--yes")

    assert result.exit_code == 1
    assert "Study 's-1' not found or not visible to 'ops'." in result.output
    assert not patch.called


def test_delete_has_no_force_option(cp: respx.MockRouter, ops: store.Identity) -> None:
    # An unknown option is answered with the command's --help and exit 0, by design
    # (HelpOnErrorGroup, vl/lib/cli.py), not with click's usage error.
    result = _invoke("delete", "globo", STUDY_ID, "--yes", "--force")

    assert result.exit_code == 0
    assert "Usage: vl study delete" in result.output
    assert "--force" not in result.output
    assert len(cp.calls) == 0


def test_delete_as_selects_the_identity(cp: respx.MockRouter, ops: store.Identity) -> None:
    boss = add_user("boss", orgs=[{"orgId": "org-1", "role": "ORG_ADMIN"}], default=False)
    _lookup(cp, _dto())
    _patch(cp)

    result = _invoke("delete", "globo", STUDY_ID, "--yes", "--as", "boss")

    assert result.exit_code == 0, result.output
    assert len(cp.calls) == 2
    for call in cp.calls:
        assert call.request.headers["Authorization"] == f"Bearer {_token(boss)}"


# --------------------------------------------------------------------------- #
# every command needs an identity (§3)
# --------------------------------------------------------------------------- #

NO_IDENTITY_COMMANDS = [
    ["delete", "globo", STUDY_ID, "--yes"],
]


@pytest.mark.parametrize("args", NO_IDENTITY_COMMANDS, ids=lambda a: a[0])
def test_every_command_needs_an_identity(cp: respx.MockRouter, args: list[str]) -> None:
    store.ensure_local_environment_seeded()

    result = _invoke(*args)

    assert result.exit_code == 1
    assert "vl usr-acct use" in result.output
    assert len(cp.calls) == 0


@pytest.mark.parametrize("args", NO_IDENTITY_COMMANDS, ids=lambda a: a[0])
def test_an_unknown_as_label_is_refused(cp: respx.MockRouter, ops: store.Identity, args: list[str]) -> None:
    result = _invoke(*args, "--as", "nobody")

    assert result.exit_code == 1
    assert "nobody" in result.output
    assert len(cp.calls) == 0
