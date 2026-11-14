"""The remote path of `vl network|org|node start|stop|reset` (vl-agent-cli-spec.md §2, §7.1, §7.2).

When some node in scope is on another host (or ``--remote`` asks for it), the whole
verb is one command through the control plane: the role check has been made
locally by the verb, the command is ``POST``ed with the operator's token, recorded in
``issued_command`` on ``202``, the hints of §7.2 are printed, and then `vl` waits
for the results (vl.commands.wait) unless ``--no-wait`` was given.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import typer

from vl.commands import scope as scope_mod
from vl.commands import wait as wait_mod
from vl.commands._shared import CliError, print_error
from vl.lib import api, auth, proc, store
from vl.lib.output import console, note, warn

VERB_TYPES = {"start": "START", "stop": "STOP", "reset": "RESET"}

NO_WAIT_HINT = "Look at the result: vl command show --last   Wait for it: vl command watch --last"


class IssueUnreachable(CliError):
    """The control plane could not be reached to issue the command (exit 4)."""


@dataclass(frozen=True)
class Issued:
    correlation_id: str
    command_type: str
    command_text: str
    expires_at: str
    wait_until: str
    targets: list[dict[str, Any]]
    skipped: list[dict[str, Any]]


def command_text(kind: str, verb: str) -> str:
    """The command as typed, without its arguments: ``vl network stop``."""
    return f"vl {kind.lower()} {verb}"


def _path(kind: str, organization: store.Organization | None, org_node: int | None) -> str:
    if kind == "NETWORK":
        return "/v1/network/commands"
    assert organization is not None
    if kind == "ORG":
        return f"/v1/orgs/{organization.server_org_id}/commands"
    return f"/v1/orgs/{organization.server_org_id}/nodes/{org_node}/commands"


def org_namer(environment: store.Environment) -> Callable[[str], str]:
    names = {o.server_org_id: o.name for o in store.list_organizations(environment.name)}
    return lambda org_id: names.get(org_id, org_id)


def _node_labels(entries: list[dict[str, Any]], name: Callable[[str], str]) -> list[str]:
    return [f"{name(str(e.get('orgId')))}/{e.get('orgNode')}" for e in entries]


def issue(environment: store.Environment, caller: store.Identity, kind: str, verb: str,
          organization: store.Organization | None, org_node: int | None, *,
          include_snapshots: bool = False) -> Issued:
    """``POST`` the command; on ``202`` record it in ``issued_command``.

    A refusal is a ``CliError`` (exit 1) with the control plane's reason; not being
    able to reach it is :class:`IssueUnreachable` (exit 4).
    """
    command_type = VERB_TYPES[verb]
    text = command_text(kind, verb)
    body: dict[str, Any] = {"type": command_type, "command": text}
    if command_type == "RESET" and include_snapshots:
        body["options"] = {"includeSnapshots": True}
    path = _path(kind, organization, org_node)
    name = org_namer(environment)
    try:
        response: dict[str, Any] = auth.authed_call(
            caller, environment.idp_base_url, lambda c: c.post(path, json=body), environment.cp_base_url)
    except api.ApiError as exc:
        if exc.status_code == 0 or exc.status_code >= 500:
            raise IssueUnreachable(
                f"The control plane could not be reached to issue the command ({exc}). Whether it was "
                f"published is not known: see vl command list.") from exc
        reason = exc.context.get("reason")
        if exc.status_code == 409 and reason == "NO_TARGETS":
            skipped = _node_labels(list(exc.context.get("skipped") or []), name)
            raise CliError(f"{exc.detail}"
                           + (f" No agent for: {', '.join(skipped)}." if skipped else "")) from exc
        if exc.status_code == 409 and reason == "NOT_DOWN":
            nodes = [f"{n.get('orgName') or name(str(n.get('orgId')))}/{n.get('orgNode')} ({n.get('state')})"
                     for n in exc.context.get("nodes") or []]
            raise CliError("Every node must be DOWN before a reset; nothing was published. Not down: "
                           + ", ".join(nodes)) from exc
        raise
    issued = Issued(
        correlation_id=str(response["correlationId"]),
        command_type=command_type,
        command_text=text,
        expires_at=str(response.get("expiresAt")),
        wait_until=str(response.get("waitUntil")),
        targets=list(response.get("targets") or []),
        skipped=list(response.get("skipped") or []),
    )
    store.record_issued_command(
        environment.name, issued.correlation_id, command_type, kind,
        organization.name if organization is not None else None, org_node,
        datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), issued.expires_at)
    return issued


def _published_line(kind: str, verb: str, organization: store.Organization | None, org_node: int | None,
                    issued: Issued) -> str:
    agents = len({t.get("agentId") for t in issued.targets})
    what = ("Network" if kind == "NETWORK" else
            f"Org {organization.name}" if kind == "ORG" and organization else
            f"Node {organization.name}/{org_node}" if organization else "Node")
    plural = "s" if agents != 1 else ""
    nodes = len(issued.targets)
    return (f"{what} {verb} published to {agents} agent{plural} ({nodes} node{'s' if nodes != 1 else ''}). "
            f"Command {issued.correlation_id}")


def _orgs_in_command(environment: store.Environment, kind: str,
                     organization: store.Organization | None) -> list[str]:
    """The local org names a command can reach: the org, or every org for the network."""
    if organization is not None:
        return [organization.name]
    return [o.name for o in store.list_organizations(environment.name)]


def print_hints(environment: store.Environment, kind: str, organization: store.Organization | None,
                org_node: int | None, issued: Issued) -> None:
    """§7.2, computed locally: the skipped nodes, this host's NEW nodes in scope, and a
    local agent that is not running for an org in scope."""
    name = org_namer(environment)
    if issued.skipped:
        console.print(f"  Skipped (no agent): {', '.join(_node_labels(issued.skipped, name))}")
    orgs = _orgs_in_command(environment, kind, organization)
    new = [n for n in store.get_nodes(environment) if n.org_name in orgs and n.node_state == "NEW"
           and (org_node is None or n.org_node == org_node)]
    if new:
        console.print("  Never started on this machine, so not reachable by remote commands: "
                      + ", ".join(f"{n.org_name}/{n.org_node}" for n in new))
    for agent in store.list_agents(environment.name):
        if agent.org_name not in orgs:
            continue
        label = store.get_identity_by_id(agent.svc_acct_id).label
        if not (agent.state == "RUNNING" and proc.is_agent_process(agent.pid, label)):
            warn(f"Warning: the agent for org {agent.org_name} on this host is not running; its nodes will "
                 f"not answer. Start it with vl agent start {agent.org_name}.")


def run(environment: store.Environment, caller: store.Identity, kind: str, verb: str,
        organization: store.Organization | None = None, org_node: int | None = None, *,
        why: str, no_wait: bool, include_snapshots: bool = False) -> int:
    """The whole remote path; returns the exit status (vl-agent-cli-spec.md §7.6)."""
    note(why)
    sources = wait_mod.Sources.before_request(environment, kind, organization)
    try:
        issued = issue(environment, caller, kind, verb, organization, org_node,
                       include_snapshots=include_snapshots)
    except IssueUnreachable as exc:
        print_error(exc)
        return wait_mod.EXIT_UNREACHABLE
    console.print(_published_line(kind, verb, organization, org_node, issued))
    print_hints(environment, kind, organization, org_node, issued)
    if no_wait:
        console.print(NO_WAIT_HINT)
        return 0
    return wait_mod.wait(environment, caller, issued.correlation_id, issued.targets, issued.wait_until,
                         sources=sources)


def decide(environment: store.Environment, caller: store.Identity, *,
           organization: store.Organization | None = None, org_node: int | None = None,
           local: bool, remote: bool) -> str | None:
    """Whether the verb goes remote: ``None`` for the local loops, else the line that says why.

    ``--local`` never asks the control plane; ``--remote`` always goes remote. An
    unreachable control plane prints the §2 error and exits 4, changing nothing.
    """
    if local and remote:
        raise typer.BadParameter("--local and --remote cannot be combined", param_hint="--local")
    if local:
        return None
    if remote:
        return "--remote: sending the command through the control plane."
    try:
        resolved = scope_mod.resolve_scope(environment, caller, organization=organization, org_node=org_node)
    except scope_mod.ScopeUnreachable as exc:
        print_error(exc)
        raise typer.Exit(scope_mod.EXIT_UNREACHABLE) from exc
    moved = resolved.moved_note()
    if moved:
        note(moved)
    return resolved.why_remote() if resolved.remote else None


def confirm_reset(what: str, include_snapshots: bool) -> bool:
    """The reset prompt for the remote path; refuses to prompt without a terminal."""
    import sys

    if not sys.stdin.isatty():
        raise CliError("a reset requires confirmation; pass --yes to run non-interactively")
    dirs = "data and snapshots directories" if include_snapshots else "data directories"
    return typer.confirm(f"Reset the {dirs} of {what}? This permanently deletes their local blockchain "
                         f"state. Are you sure?", default=False)
