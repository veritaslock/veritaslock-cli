"""Tests for the `vl study` command group (vl-study-spec.md §1-§3).

The control plane is stubbed with respx: every route a test doesn't register is
refused (``assert_all_mocked``), so "nothing sent" is checked by counting the calls
the router saw.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path
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
# update (§2.4)
# --------------------------------------------------------------------------- #


def test_update_resolves_then_patches_with_the_study_id(cp: respx.MockRouter, ops: store.Identity) -> None:
    _lookup(cp, _dto())
    patch = _patch(cp, httpx.Response(200, json=_dto(studyDescription="Updated")))

    result = _invoke("update", "globo", STUDY_ID, "--description", "Updated")

    assert result.exit_code == 0, result.output
    assert [call.request.method for call in cp.calls] == ["GET", "PATCH"]
    _assert_lookup(cp.calls[0].request)
    request = patch.calls.last.request
    assert request.url.path == f"/v1/studies/{EVENT_ID}"
    assert _query(request) == {"studyId": STUDY_ID}
    assert request.headers["Authorization"] == f"Bearer {_token(ops)}"
    assert "Updated Study s-1" in result.output


@pytest.mark.parametrize(
    ("args", "body"),
    [
        (["--study-name", "Flu 2027"], {"studyName": "Flu 2027"}),
        (["--description", "New text"], {"studyDescription": "New text"}),
        (["--starting-time", "2026-11-02T00:00:00Z"], {"startingTime": "2026-11-02T00:00:00Z"}),
        (["--ending-time", "2026-12-31T00:00:00Z"], {"endingTime": "2026-12-31T00:00:00Z"}),
        (["--allow-multiple-events-per-client"], {"allowMultipleEventsPerClient": True}),
        (["--no-allow-multiple-events-per-client"], {"allowMultipleEventsPerClient": False}),
        (["--disable"], {"state": "DISABLED"}),
        (["--enable"], {"state": "ENABLED"}),
        (
            ["--study-name", "Flu 2027", "--ending-time", "2026-12-31T00:00:00Z"],
            {"studyName": "Flu 2027", "endingTime": "2026-12-31T00:00:00Z"},
        ),
        (
            ["--ending-time", "2027-01-31T00:00:00Z", "--enable"],
            {"endingTime": "2027-01-31T00:00:00Z", "state": "ENABLED"},
        ),
        (
            ["--description", "On hold", "--disable", "--no-allow-multiple-events-per-client"],
            {"studyDescription": "On hold", "state": "DISABLED", "allowMultipleEventsPerClient": False},
        ),
    ],
)
def test_update_sends_only_the_flags_given(
    cp: respx.MockRouter, ops: store.Identity, args: list[str], body: dict[str, Any]
) -> None:
    _lookup(cp, _dto())
    patch = _patch(cp)

    result = _invoke("update", "globo", STUDY_ID, *args)

    assert result.exit_code == 0, result.output
    assert _body(patch.calls.last.request) == body


def test_update_enable_notes_that_the_state_is_pending(cp: respx.MockRouter, ops: store.Identity) -> None:
    _lookup(cp, _dto())
    _patch(cp)

    result = _invoke("update", "globo", STUDY_ID, "--enable")

    assert result.exit_code == 0, result.output
    assert "Study state update is pending" in result.output
    assert "vl study show globo s-1" in result.output


def test_update_disable_and_enable_together_is_refused_before_any_request(
    cp: respx.MockRouter, ops: store.Identity
) -> None:
    result = _invoke("update", "globo", STUDY_ID, "--disable", "--enable")

    assert result.exit_code == 1
    assert "--disable and --enable are mutually exclusive" in result.output
    assert len(cp.calls) == 0


def test_update_with_no_flags_is_refused_before_any_request(cp: respx.MockRouter, ops: store.Identity) -> None:
    result = _invoke("update", "globo", STUDY_ID)

    assert result.exit_code == 1
    assert "Must specify at least one option to be modified" in result.output
    assert len(cp.calls) == 0


@pytest.mark.parametrize(
    ("state", "args", "response", "expected"),
    [
        (
            "FINALIZED", ["--description", "x"],
            _problem(400, "A FINALIZED study can't be updated."),
            ["Error: A FINALIZED study can't be updated. (HTTP 400)"],
        ),
        (
            "ARCHIVED", ["--description", "x"],
            _problem(400, "An ARCHIVED study can't be updated."),
            ["Error: An ARCHIVED study can't be updated. (HTTP 400)"],
        ),
        (
            "DISABLED", ["--description", "x"],
            _problem(400, "The study is soft-deleted and can no longer be modified."),
            ["soft-deleted and can no longer be modified. (HTTP 400)"],
        ),
        (
            "ACTIVE", ["--disable"],
            _problem(400, "The study's endingTime has passed; it can no longer be updated."),
            ["endingTime has passed; it can no longer be updated. (HTTP 400)"],
        ),
        (
            "DISABLED", ["--ending-time", "2026-01-01T00:00:00Z"],
            _problem(
                400, "Validation failed.",
                invalidFields=[{"field": "endingTime", "message": "must be in the future"}],
            ),
            ["Error: Validation failed. (HTTP 400)", "endingTime: must be in the future"],
        ),
        (
            "DISABLED", ["--study-name", "Taken"],
            _problem(409, "A study named 'Taken' already exists in this organization."),
            ["already exists in this organization. (HTTP 409)"],
        ),
    ],
)
def test_update_reports_the_server_refusal(
    cp: respx.MockRouter,
    ops: store.Identity,
    state: str,
    args: list[str],
    response: httpx.Response,
    expected: list[str],
) -> None:
    _lookup(cp, _dto(studyState=state))
    patch = _patch(cp, response)

    result = _invoke("update", "globo", STUDY_ID, *args)

    assert result.exit_code == 1
    assert patch.call_count == 1
    for text in expected:
        assert text in result.output
    assert "Updated Study" not in result.output


def test_update_of_a_soft_deleted_study_still_reaches_the_server(cp: respx.MockRouter, ops: store.Identity) -> None:
    # The lookup includes deleted studies, so the server's own refusal is what's reported.
    _lookup(cp, _dto(deleted=True))
    patch = _patch(cp, _problem(400, "The study is soft-deleted and can no longer be modified."))

    result = _invoke("update", "globo", STUDY_ID, "--description", "x")

    assert result.exit_code == 1
    assert patch.call_count == 1
    assert "soft-deleted" in result.output
    assert "not found" not in result.output


def test_update_of_a_rejected_study_is_an_ordinary_patch(cp: respx.MockRouter, ops: store.Identity) -> None:
    rejected = _dto(studyState="REJECTED", rejectedReason="startingTime is in the past")
    _lookup(cp, rejected)
    patch = _patch(cp, httpx.Response(200, json=_dto(studyState="SUBMITTED")))

    result = _invoke("update", "globo", STUDY_ID, "--starting-time", "2026-11-05T00:00:00Z")

    assert result.exit_code == 0, result.output
    assert _body(patch.calls.last.request) == {"startingTime": "2026-11-05T00:00:00Z"}
    assert "SUBMITTED" in result.output


def test_update_of_an_unknown_study_is_not_found(cp: respx.MockRouter, ops: store.Identity) -> None:
    _lookup(cp)
    patch = _patch(cp)

    result = _invoke("update", "globo", STUDY_ID, "--description", "x")

    assert result.exit_code == 1
    assert "Study 's-1' not found or not visible to 'ops'." in result.output
    assert not patch.called


def test_update_refuses_a_study_in_another_org(cp: respx.MockRouter, ops: store.Identity) -> None:
    _lookup(cp, _dto(organization="acme"))
    patch = _patch(cp)

    result = _invoke("update", "globo", STUDY_ID, "--description", "x")

    assert result.exit_code == 1
    assert "study s-1 belongs to org acme, not globo." in result.output
    assert not patch.called


@pytest.mark.parametrize("option", ["--delete", "--deleted"])
def test_update_has_no_delete_option(cp: respx.MockRouter, ops: store.Identity, option: str) -> None:
    # Unknown option: --help and exit 0 (HelpOnErrorGroup), nothing sent.
    result = _invoke("update", "globo", STUDY_ID, option)

    assert result.exit_code == 0
    assert "Usage: vl study update" in result.output
    assert "--delete" not in result.output
    assert len(cp.calls) == 0


def test_update_as_selects_the_identity(cp: respx.MockRouter, ops: store.Identity) -> None:
    boss = add_user("boss", orgs=[{"orgId": "org-1", "role": "ORG_ADMIN"}], default=False)
    _lookup(cp, _dto())
    _patch(cp)

    result = _invoke("update", "globo", STUDY_ID, "--disable", "--as", "boss")

    assert result.exit_code == 0, result.output
    for call in cp.calls:
        assert call.request.headers["Authorization"] == f"Bearer {_token(boss)}"


# --------------------------------------------------------------------------- #
# add (§2.1)
# --------------------------------------------------------------------------- #

PAYLOAD: dict[str, Any] = {
    "studyId": STUDY_ID,
    "studyName": "Flu 2026",
    "studyDescription": "Seasonal flu",
    "orgName": "globo",
    "teamName": "ingest",
    "startingTime": "2026-11-01T00:00:00Z",
    "endingTime": "2026-12-01T00:00:00Z",
    "allowMultipleEventsPerClient": False,
    "schema": {"fields": [{"name": "temp", "type": "double"}]},
}


def _study_file(tmp_path: Path, payload: Any = None, *, text: str | None = None) -> Path:
    path = tmp_path / "study.json"
    path.write_text(text if text is not None else json.dumps(PAYLOAD if payload is None else payload))
    return path


def _post(cp: respx.MockRouter, response: httpx.Response | None = None) -> respx.Route:
    return cp.post(f"{CP}/v1/studies").mock(
        return_value=response or httpx.Response(201, json=_dto(studyState="SUBMITTED"))
    )


def test_add_posts_the_file_as_is(cp: respx.MockRouter, ops: store.Identity, tmp_path: Path) -> None:
    post = _post(cp)

    result = _invoke("add", "globo", str(_study_file(tmp_path)))

    assert result.exit_code == 0, result.output
    assert len(cp.calls) == 1
    request = post.calls.last.request
    assert _body(request) == PAYLOAD
    assert _query(request) == {}
    assert request.headers["Authorization"] == f"Bearer {_token(ops)}"
    assert "Study Created" in result.output
    assert "SUBMITTED" in result.output


def test_add_resubmits_a_rejected_study_with_the_same_command(
    cp: respx.MockRouter, ops: store.Identity, tmp_path: Path
) -> None:
    # The server decides whether it's a new study or a resubmission; vl sends the same request.
    post = _post(cp, httpx.Response(200, json=_dto(eventId="ev-2", studyState="SUBMITTED")))

    result = _invoke("add", "globo", str(_study_file(tmp_path)))

    assert result.exit_code == 0, result.output
    assert _body(post.calls.last.request) == PAYLOAD
    assert len(cp.calls) == 1


def test_add_refuses_an_org_that_differs_from_the_files(
    cp: respx.MockRouter, ops: store.Identity, tmp_path: Path
) -> None:
    result = _invoke("add", "acme", str(_study_file(tmp_path)))

    assert result.exit_code == 1
    assert "orgName in file (globo) does not match acme specified on cmd line." in result.output
    assert len(cp.calls) == 0


def test_add_refuses_a_file_without_an_org_name(
    cp: respx.MockRouter, ops: store.Identity, tmp_path: Path
) -> None:
    payload = {k: v for k, v in PAYLOAD.items() if k != "orgName"}

    result = _invoke("add", "globo", str(_study_file(tmp_path, payload)))

    assert result.exit_code == 1
    assert "study.json does not contain an orgName." in result.output
    assert len(cp.calls) == 0


def test_add_refuses_invalid_json(cp: respx.MockRouter, ops: store.Identity, tmp_path: Path) -> None:
    result = _invoke("add", "globo", str(_study_file(tmp_path, text='{"orgName": "globo",')))

    assert result.exit_code == 1
    assert "Invalid JSON: study.json - 1:" in result.output
    assert len(cp.calls) == 0


def test_add_refuses_a_missing_file(cp: respx.MockRouter, ops: store.Identity, tmp_path: Path) -> None:
    # Checked by click (exists=True) before the command runs: a usage error, exit 2.
    result = _invoke("add", "globo", str(tmp_path / "nope.json"))

    assert result.exit_code == 2
    assert "does not exist" in result.output
    assert len(cp.calls) == 0


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read a mode-000 file")
def test_add_refuses_an_unreadable_file(cp: respx.MockRouter, ops: store.Identity, tmp_path: Path) -> None:
    # Checked by click (readable=True) before the command runs: a usage error, exit 2.
    path = _study_file(tmp_path)
    path.chmod(0)
    try:
        result = _invoke("add", "globo", str(path))
    finally:
        path.chmod(0o600)

    assert result.exit_code == 2
    assert "is not readable" in result.output
    assert len(cp.calls) == 0


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (
            _problem(
                400, "Validation failed.",
                invalidFields=[{"field": "endingTime", "message": "must be in the future"}],
            ),
            ["Error: Validation failed. (HTTP 400)", "endingTime: must be in the future"],
        ),
        (
            _problem(409, "A study with studyId 's-1' already exists."),
            ["Error: A study with studyId 's-1' already exists. (HTTP 409)"],
        ),
        (
            _problem(409, "A study named 'Flu 2026' already exists in this organization."),
            ["already exists in this organization. (HTTP 409)"],
        ),
        (
            _problem(409, "Study 's-1' was rejected and deleted; its studyId stays taken."),
            ["its studyId stays taken. (HTTP 409)"],
        ),
        (
            _problem(403, "Requires ORG_ADMIN of the study's org or TEAM_ADMIN of its team."),
            ["Error: Requires ORG_ADMIN of the study's org or TEAM_ADMIN of its team. (HTTP 403)"],
        ),
    ],
)
def test_add_reports_the_server_refusal(
    cp: respx.MockRouter,
    ops: store.Identity,
    tmp_path: Path,
    response: httpx.Response,
    expected: list[str],
) -> None:
    post = _post(cp, response)

    result = _invoke("add", "globo", str(_study_file(tmp_path)))

    assert result.exit_code == 1
    assert post.call_count == 1
    for text in expected:
        assert text in result.output
    assert "Study Created" not in result.output


def test_add_retries_a_401_once_with_a_fresh_token_then_reports_it(
    cp: respx.MockRouter, ops: store.Identity, tmp_path: Path
) -> None:
    login = cp.post(f"{IDP}/auth/user/login").mock(
        return_value=httpx.Response(200, json={"accessToken": "fresh-jwt", "expiresIn": 900})
    )
    post = _post(cp, _problem(401, "Token expired."))
    stale = _token(ops)

    result = _invoke("add", "globo", str(_study_file(tmp_path)))

    assert result.exit_code == 1
    assert "Error: Token expired. (HTTP 401)" in result.output
    assert login.call_count == 1
    assert [c.request.headers["Authorization"] for c in post.calls] == [
        f"Bearer {stale}",
        "Bearer fresh-jwt",
    ]


def test_add_as_selects_the_identity(cp: respx.MockRouter, ops: store.Identity, tmp_path: Path) -> None:
    boss = add_user("boss", orgs=[{"orgId": "org-1", "role": "ORG_ADMIN"}], default=False)
    post = _post(cp)

    result = _invoke("add", "globo", str(_study_file(tmp_path)), "--as", "boss")

    assert result.exit_code == 0, result.output
    assert post.calls.last.request.headers["Authorization"] == f"Bearer {_token(boss)}"


def test_add_needs_an_identity(cp: respx.MockRouter, tmp_path: Path) -> None:
    store.ensure_local_environment_seeded()

    result = _invoke("add", "globo", str(_study_file(tmp_path)))

    assert result.exit_code == 1
    assert "vl usr-acct use" in result.output
    assert len(cp.calls) == 0


# --------------------------------------------------------------------------- #
# every command needs an identity (§3)
# --------------------------------------------------------------------------- #

NO_IDENTITY_COMMANDS = [
    ["delete", "globo", STUDY_ID, "--yes"],
    ["update", "globo", STUDY_ID, "--disable"],
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
