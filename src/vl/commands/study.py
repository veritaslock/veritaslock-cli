"""`vl study` — manage VeritasLock studies.

Authenticates with Phase 3's `--as` / `VL_IDENTITY` / default identity + cached
token flow throughout. See vl-study-spec.md.
"""

from __future__ import annotations

import json
import sys
from typing import Annotated, Any

import typer

from vl.commands._shared import (
    AsOption,
    EnvOption,
    report_errors,
    CliError,
)
from vl.lib import store, roles, api, auth
from vl.lib.cli import HelpOnErrorGroup
from vl.lib.output import render, note
from pathlib import Path

app = typer.Typer(
    help="Manage VeritasLock studies.",
    no_args_is_help=True,
    cls=HelpOnErrorGroup
)


# --------------------------------------------------------------------------- #
# study
# --------------------------------------------------------------------------- #


def get_json(file_path: Path) -> Any:
    try:
        with open(file_path) as f:
            data = json.load(f)
            return data
    except PermissionError:
        raise CliError(f"Permission denied: {file_path}")

def _study_row(dto: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": dto["studyName"],
        "study_id": dto["studyId"],
        "state": dto["studyState"],
        "organization": dto["organization"],
        "starting_time": dto["startingTime"],
        "ending_time": dto["endingTime"],
        "deleted": dto["deleted"],
    }


@app.command("add")
def add(
    org: Annotated[str, typer.Argument(help="Organization name.")],
    file: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True, help="file containing study payload.")],
    env: EnvOption = None,
    as_: AsOption = None,
) -> None:
    """Create a study or can be used to resubmit a rejected study."""
    with report_errors():
        environment = store.get_environment(env)
        caller = store.resolve_identity(environment.name, as_)
        try:
            payload = get_json(file)
        except json.JSONDecodeError as e:
            raise CliError(f"Invalid JSON: {file.name} - {e.lineno}:{e.colno}")
        if isinstance(payload, dict):
            file_org_name = payload.get("orgName")
            if file_org_name != org:
                if file_org_name is None:
                    msg = f"{file.name} does not contain an orgName."
                else:
                    msg = f"orgName in file ({file_org_name}) does not match {org} specified on cmd line."
                raise CliError(msg)

        result: dict[str, Any] = auth.authed_call(
            caller,
            environment.idp_base_url,
            lambda c: c.post("/v1/studies", json=payload),
            environment.cp_base_url
            )
        render(_study_row(result), title="Study Created")


@app.command("show")
def show(
    org: Annotated[str, typer.Argument(help="Organization name.")],
    study_id: Annotated[str, typer.Argument(help="StudyId.")],
    env: EnvOption = None,
    as_: AsOption = None,
) -> None:
    """Show a study."""
    with report_errors():
        environment = store.get_environment(env)
        caller = store.resolve_identity(environment.name, as_)
        dto = get_study_dto(environment, caller, org, study_id)

        study = full_study_dto(dto)
        render(study, title=f"Study {study_id}")


def get_study_dto(environment: store.Environment, caller: store.Identity, org: str, study_id: str) -> dict[str, Any]:
    body: dict[str, Any] = auth.authed_call(
        caller,
        environment.idp_base_url,
        lambda c: c.get("/v1/studies", params={"studyId": study_id, "includeDeleted": True}),
        environment.cp_base_url
    )
    items: list[dict[str, Any]] = body.get("items", [])
    if not items:
        raise store.StudyNotFoundError(
            f"Study {study_id!r} not found or not visible to {caller.label!r}."
        )
    dto = items[0]
    returned_org = dto["organization"]
    if returned_org != org:
        raise CliError(f"study {study_id} belongs to org {returned_org}, not {org}.")

    return dto


def full_study_dto(dto: dict[str, Any]) -> dict[str, Any]:
    row: dict[str, Any] = {
        "event_id": dto["eventId"],
        "name": dto["studyName"],
        "description": dto["studyDescription"],
        "study_id": dto["studyId"],
        "state": dto["studyState"],
        "rejected_reason": dto["rejectedReason"],
        "organization": dto["organization"],
        "starting_time": dto["startingTime"],
        "ending_time": dto["endingTime"],
        "client_id": dto["clientId"],
        "created_at": dto["createdAt"],
        "created_by": dto["createdBy"],
        "allows_multiple": dto["allowMultipleEventsPerClient"],
        "deleted": dto["deleted"],
    }
    return row


def get_list_params(study_id: str | None, study_name: str | None, include_deleted: bool) -> dict[str, Any]:
    params : dict[str, Any] = {}
    if study_id is not None:
        params["studyId"] = study_id

    if study_name is not None:
        params["studyName"] = study_name

    params["includeDeleted"] = include_deleted
    return params


def get_all_studies(environment: store.Environment, caller: store.Identity, params: dict[str, Any]) -> list[dict[str, Any]]:

    studies: list[dict[str, Any]] = []
    cursor: str | None = None
    study_params = params.copy()

    while True:
        if cursor is not None:
            study_params["cursor"] = cursor
        body: dict[str, Any] = auth.authed_call(
            caller,
            environment.idp_base_url,
            lambda c: c.get("/v1/studies", params=study_params), environment.cp_base_url)
        items: list[dict[str, Any]] = body.get("items", [])
        studies.extend(items)
        cursor = body.get("nextCursor")
        if cursor is None:
            return studies


@app.command("list")
def list_(
    study_id: Annotated[
        str | None, typer.Option("--study-id", help="Filter by study id.")
    ] = None,
    study_name: Annotated[
        str | None, typer.Option("--study-name", help="Filter by study name.")
    ] = None,
    include_deleted: Annotated[
        bool, typer.Option("--include-deleted", help="Include studies which were soft deleted.")
    ] = False,
    env: EnvOption = None,
    as_: AsOption = None,
) -> None:
    """List studies."""
    with report_errors():
        params = get_list_params(study_id, study_name, include_deleted)
        environment = store.get_environment(env)
        caller = store.resolve_identity(environment.name, as_)
        studies = get_all_studies(environment, caller, params)
        rows = [_study_row(study) for study in studies]

        render(rows, title="Studies")


def build_update_json(study_name: str | None, descr: str | None, starting_time: str | None, ending_time: str | None,
                      allow_multiple: bool | None, disable: bool |None, enable: bool | None, ) -> dict[str, Any]:

    state = None
    if disable:
        state = "DISABLED"
    elif enable:
        state = "ENABLED"

    payload = {
        key: value
        for key, value in {
            "studyName": study_name,
            "studyDescription": descr,
            "startingTime": starting_time,
            "endingTime": ending_time,
            "allowMultipleEventsPerClient": allow_multiple,
            "state": state
        }.items()
        if value is not None
    }
    return payload


def validate_input(study_name: str | None, descr: str | None, starting_time: str | None, ending_time: str | None,
                      allow_multiple: bool | None, disable: bool |None, enable: bool | None, ) -> None:
    if all(p is None for p in (study_name, descr, starting_time, ending_time, allow_multiple, disable, enable)):
        raise CliError("Must specify at least one option to be modified")

    if disable and enable:
        raise CliError("--disable and --enable are mutually exclusive")


@app.command("update")
def update(
    org: Annotated[str, typer.Argument(help="Organization name.")],
    study_id: Annotated[str, typer.Argument(help="StudyId.")],
    study_name: Annotated[
        str | None, typer.Option("--study-name", help="Name of the study.")
    ] = None,
    description: Annotated[
        str | None, typer.Option("--description", help="Description of the study.")
    ] = None,
    starting_time: Annotated[
        str | None, typer.Option("--starting-time", help="Starting time of the study in ISO-8601 format.")
    ] = None,
    ending_time: Annotated[
        str | None, typer.Option("--ending-time", help="Ending time of the study in ISO-8601 format.")
    ] = None,
    allows_multiple:  Annotated[
        bool | None, typer.Option("--allow-multiple-events-per-client/--no-allow-multiple-events-per-client", help="Allow multiple events per client.")
    ] = None,
    disable: Annotated[
        bool | None, typer.Option("--disable", help="Disables the study.")
    ] = None,
    enable: Annotated[
        bool | None, typer.Option("--enable", help="Enables a previously disabled study.")
    ] = None,
    env: EnvOption = None,
    as_: AsOption = None,
) -> None:
    """Update a study."""
    with report_errors():
        validate_input(study_name, description, starting_time, ending_time, allows_multiple, disable, enable)
        environment = store.get_environment(env)
        caller = store.resolve_identity(environment.name, as_)
        study_dto = get_study_dto(environment, caller, org, study_id)
        event_id = study_dto["eventId"]
        update_json = build_update_json(study_name, description, starting_time, ending_time, allows_multiple, disable, enable)
        updated_study_dto: dict[str, Any] = auth.authed_call(
            caller,
            environment.idp_base_url,
            lambda c: c.patch(f"/v1/studies/{event_id}", params={"studyId": study_id}, json=update_json),
            environment.cp_base_url
        )
        study = full_study_dto(updated_study_dto)
        render(study, title=f"Updated Study {study_id}")
        if enable is not None:
            note(f"Study state update is pending. Please check with `vl study show {org} {study_id}`")


@app.command("delete")
def delete(
    org: Annotated[str, typer.Argument(help="Organization name.")],
    study_id: Annotated[str, typer.Argument(help="StudyId.")],
    force: Annotated[
        bool, typer.Option("--force", help="Performs a hard delete.")
    ] = False,
    yes: Annotated[
        bool | None, typer.Option("--yes", help="Perform delete without prompting the user.")
    ] = None,
    env: EnvOption = None,
    as_: AsOption = None,
) -> None:
    """Delete a study."""
    with report_errors():
        environment = store.get_environment(env)
        caller = store.resolve_identity(environment.name, as_)
        study_dto = get_study_dto(environment, caller, org, study_id)
        deleted = study_dto["deleted"]
        state = study_dto["studyState"]
        if deleted and not force:
            raise CliError("Study was already soft-deleted; use --force to remove it permanently")

        if yes is None:
            if not sys.stdin.isatty():
                raise CliError("vl study delete requires confirmation; pass --yes to run non-interactively")

            if force:
                msg = f"Delete {org}/{study_id}? This permanently removes the study from the control plane. Are you sure?"
            else:
                msg = f"Delete {org}/{study_id}? This removes the study from the control plane. Are you sure?"

            yes = typer.confirm(
                  msg,
                  default=False,
            )

        if yes:
            event_id = study_dto["eventId"]

            if force:
                auth.authed_call(
                    caller,
                    environment.idp_base_url,
                    lambda c: c.delete(f"/v1/studies/{event_id}", params={"studyId": study_id, "force": force}),
                    environment.cp_base_url
                )
                note(f"Study {study_id} was permanently deleted.")
            else:
                delete_patch: dict[str, Any] = {"deleted": True}
                auth.authed_call(
                    caller,
                    environment.idp_base_url,
                    lambda c: c.patch(f"/v1/studies/{event_id}", params={"studyId": study_id}, json=delete_patch),
                    environment.cp_base_url
                )
                note(f"Study {study_id} was deleted.")

