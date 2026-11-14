"""`vl command` — commands issued through the control plane (vl-agent-cli-spec.md §8).

- ``show [<id> | --last]``: ``GET /v1/commands/{id}``: a header (type, scope, issuer,
  issued, expires, status and counts) and every target, skipped ones included. Exit
  0 for a command that exists, 1 for an unknown id, 4 if the control plane cannot
  be reached.
- ``watch [<id> | --last] [--timeout] [--interval]``: the wait of the remote verbs
  (vl.commands.wait), with their exit statuses (§7.6); an explicit ``--timeout`` that
  runs out before every target has answered is 3.
- ``list [--org] [--limit]``: ``GET /v1/commands``, newest first.

``--last`` is the newest ``issued_command`` row of the environment.
"""

from __future__ import annotations

from typing import Annotated, Any

import typer

from vl.commands import exits
from vl.commands import wait as wait_mod
from vl.commands._shared import AsOption, CliError, EnvOption, print_error, report_errors
from vl.lib import api, auth, store
from vl.lib.cli import HelpOnErrorGroup
from vl.lib.output import console, is_json, render

app = typer.Typer(
    help="Show, watch and list commands issued through the control plane.",
    no_args_is_help=True,
    cls=HelpOnErrorGroup,
)

IdArgument = Annotated[str | None, typer.Argument(help="The command's id (its correlationId).")]
LastOption = Annotated[bool, typer.Option("--last", help="The newest command issued from this machine.")]


def _command_id(environment: store.Environment, command_id: str | None, last: bool) -> str:
    if command_id is not None and last:
        raise typer.BadParameter("give an id or --last, not both", param_hint="--last")
    if command_id is not None:
        return command_id
    if not last:
        raise typer.BadParameter("give a command id, or --last", param_hint="ID")
    issued = store.list_issued_commands(environment.name)
    if not issued:
        raise CliError(f"No command has been issued from this machine in environment {environment.name!r}.")
    return issued[0].correlation_id


def _fetch(environment: store.Environment, caller: store.Identity, correlation_id: str) -> dict[str, Any]:
    """The command, or an exit: 1 for an unknown (or invisible) id, 4 unreachable."""
    try:
        return wait_mod.get_command(environment, caller, correlation_id)
    except api.ApiError as exc:
        if wait_mod.unreachable(exc):
            print_error(CliError(f"The control plane could not be reached ({exc})."))
            raise typer.Exit(exits.EXIT_UNREACHABLE) from exc
        if exc.status_code in (400, 404):
            print_error(CliError(f"No command {correlation_id} (or not one you may see)."))
            raise typer.Exit(1) from exc
        raise


def _issuer(environment: store.Environment, issued_by: Any) -> str:
    if not issued_by:
        return "-"
    local = store.get_identity_by_server_id(environment.name, str(issued_by), "USER")
    return local.label if local is not None else str(issued_by)


def _agents(environment: store.Environment, caller: store.Identity) -> dict[str, dict[str, Any]]:
    """The agents' host and last heartbeat, best effort (the targets carry neither)."""
    try:
        body: dict[str, Any] = auth.authed_call(
            caller, environment.idp_base_url, lambda c: c.get("/v1/agents", params={"limit": 500}),
            environment.cp_base_url)
    except (api.ApiError, auth.AuthError):
        return {}
    return {str(a.get("id")): a for a in body.get("items") or []}


@app.command("show")
def show(
    command_id: IdArgument = None,
    last: LastOption = False,
    env: EnvOption = None,
    as_: AsOption = None,
) -> None:
    """Show a command: its status, its counts and every target."""
    with report_errors():
        environment = store.get_environment(env)
        correlation_id = _command_id(environment, command_id, last)
        caller = store.resolve_identity(environment.name, as_)
        dto = _fetch(environment, caller, correlation_id)
        agents = _agents(environment, caller)
        name_of = wait_mod._org_names(environment)[0]
    if is_json():
        render(dto)
        return
    scope = str(dto.get("scope"))
    where = "" if scope == "NETWORK" else f" {name_of(str(dto.get('orgId')))}"
    if scope == "NODE":
        where += f"/{dto.get('orgNode')}"
    console.print(f"[bold]{dto.get('command') or ''}[/bold] ({dto.get('type')} {scope}{where}) "
                  f"issued by {_issuer(environment, dto.get('issuedBy'))} at {dto.get('issuedAt')}, "
                  f"expires {dto.get('expiresAt')}")
    console.print(f"Status {dto.get('status')}: {wait_mod.counts_line(dto)}")
    rows = []
    for t in dto.get("targets") or []:
        agent = agents.get(str(t.get("agentId")), {})
        state = str(t.get("state"))
        agent_state = t.get("agentState")
        if state == "NO_ANSWER" and agent_state:
            seen = agent.get("lastSeenAt")
            agent_state = f"{agent_state} (last seen {seen or 'never'})"
        rows.append({
            "agent": t.get("agentName") or "-",
            "host": agent.get("host") or "-",
            "node": f"{name_of(str(t.get('orgId')))}/{t.get('orgNode')}",
            "state": state,
            "outcome": t.get("outcome") or "-",
            "exit": t.get("exitCode"),
            "detail": t.get("detail") or "",
            "agent_state": agent_state or "",
        })
    render(rows, title=f"Command {correlation_id}")


@app.command("watch")
def watch(
    command_id: IdArgument = None,
    last: LastOption = False,
    timeout: Annotated[
        float | None, typer.Option("--timeout", min=0, help="Stop waiting after this many seconds "
                                                            "(default: until the command's waitUntil).")
    ] = None,
    interval: Annotated[
        float, typer.Option("--interval", min=0.1, help="Seconds between asks of the control plane.")
    ] = wait_mod.POLL_SECONDS,
    env: EnvOption = None,
    as_: AsOption = None,
) -> None:
    """Wait for a command's results, as the remote verbs do."""
    with report_errors():
        environment = store.get_environment(env)
        correlation_id = _command_id(environment, command_id, last)
        caller = store.resolve_identity(environment.name, as_)
        dto = _fetch(environment, caller, correlation_id)
        targets = [t for t in dto.get("targets") or [] if t.get("state") != "SKIPPED"]
        sources = wait_mod.Sources.for_issued(environment, {str(t.get("orgId")) for t in targets})
        code = wait_mod.wait(environment, caller, correlation_id, targets, str(dto.get("waitUntil")),
                             sources=sources, interval=interval, timeout=timeout)
    raise typer.Exit(code)


@app.command("list")
def list_(
    org: Annotated[str | None, typer.Option("--org", help="Only this org's commands.")] = None,
    limit: Annotated[int, typer.Option("--limit", min=1, max=500, help="How many, newest first.")] = 50,
    env: EnvOption = None,
    as_: AsOption = None,
) -> None:
    """List commands, newest first."""
    with report_errors():
        environment = store.get_environment(env)
        caller = store.resolve_identity(environment.name, as_)
        params: dict[str, Any] = {"limit": limit}
        if org is not None:
            params["orgId"] = store.get_organization(environment.name, org).server_org_id
        try:
            body: dict[str, Any] = auth.authed_call(
                caller, environment.idp_base_url, lambda c: c.get("/v1/commands", params=params),
                environment.cp_base_url)
        except api.ApiError as exc:
            if wait_mod.unreachable(exc):
                print_error(CliError(f"The control plane could not be reached ({exc})."))
                raise typer.Exit(exits.EXIT_UNREACHABLE) from exc
            raise
        name_of = wait_mod._org_names(environment)[0]
        rows = [
            {
                "id": c.get("correlationId"),
                "type": c.get("type"),
                "scope": c.get("scope"),
                "org": name_of(str(c.get("orgId"))) if c.get("orgId") else "-",
                "issuer": _issuer(environment, c.get("issuedBy")),
                "issued": c.get("issuedAt"),
                "status": c.get("status"),
            }
            for c in body.get("items") or []
        ]
    render(rows, title="Commands")
