"""`vl agent` — the node agent on this host (vl-agent-cli-spec.md §6).

All local, like `vl node`, except the control plane columns of `list`. Nothing is
copied at `create` or `start`: the agent is the installed `vl` package, run as
``python -m vl.agent`` with the same interpreter, so its directory holds only the
optional ``agent.properties`` and ``data/`` (with ``results.log``).

- ``create``: the ``NODE_AGENT`` service account, the ``agent`` row (``NEW``) and
  ``$HOME/orgs/<org>/agent/data/``. An ``ORG_ADMIN`` of the org only.
- ``start``: spawn the agent detached, check it is alive a second later.
- ``stop``: ``SIGTERM``, up to 30 seconds, then ``SIGKILL``; nodes are never touched.
- ``list``: this host's agents, with the control plane's view joined in; ``--remote``
  for every agent the control plane shows you.
- ``results``: the agent's own ``data/results.log`` and its rotated files.
"""

from __future__ import annotations

import importlib.util
import os
import secrets
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Annotated, Any

import typer
from rich import box
from rich.markup import escape
from rich.table import Table

from vl.agent import (
    EXIT_CONFIG,
    EXIT_FAILED,
    EXIT_HOST_CHANGED,
    EXIT_NOPERM,
    EXIT_UNAVAILABLE,
)
from vl.agent import config as agent_config
from vl.agent import resultslog
from vl.commands import svc_acct
from vl.commands._shared import (
    AsOption,
    CliError,
    EnvOption,
    assert_label_free,
    org_name_resolver,
    report_errors,
    require_operator,
    require_org_admin,
)
from vl.lib import api, auth, nodeops, proc, store
from vl.lib.cli import HelpOnErrorGroup
from vl.lib.output import console, is_json, note, render, warn
from vl.lib.roles import ServiceAccountRole

app = typer.Typer(
    help="Manage the node agent on this host.",
    no_args_is_help=True,
    cls=HelpOnErrorGroup,
)

# How long `start` waits before checking the agent is still alive, and how long
# `stop` gives it after SIGTERM (it finishes the node operation in hand) and then
# after SIGKILL. Module constants so the tests can shorten them.
START_CHECK_SECONDS = 1.0
STOP_TERM_SECONDS = 30.0
STOP_KILL_SECONDS = 5.0
POLL_SECONDS = 0.1

# What the agent's exit statuses mean (vl-agent-spec.md §2.4, and 70 of §8 item 7).
EXIT_HINTS: dict[int, str] = {
    EXIT_FAILED: "Startup failed; the log above says why (for example vl[agent] is not installed, or "
                 "librdkafka is older than 2.16.0).",
    EXIT_UNAVAILABLE: "The control plane, Kafka or the Schema Registry could not be reached after the "
                      "retries. Check the environment's addresses (vl env show <env>) and that the "
                      "services are up, then start it again.",
    EXIT_HOST_CHANGED: "This host's address changed since the agent registered. Start it again; it "
                       "registers with the new address.",
    EXIT_NOPERM: "Authentication was refused, or the account is not a NODE_AGENT. Check it with "
                 "vl svc-acct show <label> --remote.",
    EXIT_CONFIG: "The configuration or identity is invalid or missing; the log above says which (for "
                 "example agent.properties, the registered schema, a verilock without --print-host: "
                 "then set the host with vl env update <env> --node-host <address>).",
}

NOT_INSTALLED = (
    "The node agent needs the vl[agent] extra (confluent-kafka with Avro and the Schema Registry), "
    "which is not installed for {python}. Install vl[agent] (in this repository: "
    "uv sync --extra agent), then run vl agent start again."
)


def _org_name(environment: store.Environment, org: str) -> str:
    return store.get_organization(environment.name, org).name


def agent_label(agent: store.Agent) -> str:
    return store.get_identity_by_id(agent.svc_acct_id).label


def log_path(org: str) -> Path:
    """Where the agent's stdout and stderr go: ``$HOME/tmp/<org>-agent.out``."""
    return Path.home() / "tmp" / f"{org}-agent.out"


def agent_argv(environment: store.Environment, label: str) -> list[str]:
    """The agent's command line: the same interpreter, ``-m vl.agent``, and the
    environment's configured node host as ``--host`` when it has one."""
    argv = [sys.executable, "-m", "vl.agent", "--as", label, "--env", environment.name]
    if environment.node_host is not None:
        argv += ["--host", environment.node_host]
    return argv


def kafka_installed() -> bool:
    return importlib.util.find_spec("confluent_kafka") is not None


def is_running(agent: store.Agent, label: str) -> bool:
    return agent.state == "RUNNING" and proc.is_agent_process(agent.pid, label)


# --------------------------------------------------------------------------- #
# create
# --------------------------------------------------------------------------- #


@app.command("create")
def create(
    org: Annotated[str, typer.Argument(help="Organization name.")],
    name: Annotated[
        str | None,
        typer.Option("--name", help="The service account's name and label, which is also the agent's "
                                    "name at the control plane (default: <org>-agent)."),
    ] = None,
    env: EnvOption = None,
    as_: AsOption = None,
) -> None:
    """Create the org's agent on this host. Needs ORG_ADMIN of the org."""
    with report_errors():
        environment = store.get_environment(env)
        caller = require_org_admin(environment, as_, org)
        org_name = _org_name(environment, org)
        label = name if name is not None else f"{org_name}-agent"
        if not agent_config.NAME_PATTERN.match(label):
            raise CliError(f"The agent's name {label!r} must be 1 to 64 letters, digits, '.', '_' or '-' "
                           f"(the control plane's rule). Pass another --name.")
        try:
            existing = store.get_agent(environment.name, org_name)
        except store.AgentNotFoundError:
            pass
        else:
            raise store.AgentExistsError(
                f"Org {org_name!r} already has an agent in environment {environment.name!r} "
                f"(service account {agent_label(existing)!r}, {existing.state}). Run vl agent list to see it.")
        assert_label_free(environment.name, label)

        _, identity_id = svc_acct.create_svc_acct(
            label, f"node agent for {org_name}", secrets.token_hex(16), environment, label, org_name, caller,
            ServiceAccountRole.NODE_AGENT)
        agent = store.add_agent(environment.name, org_name, identity_id)
        data = agent_config.results_path(org_name).parent
        data.mkdir(parents=True, exist_ok=True)

    render({"org": org_name, "agent": label, "role": ServiceAccountRole.NODE_AGENT.value,
            "state": agent.state, "directory": str(data.parent)}, title="Agent created")
    note(f"Start it with: vl agent start {org_name}")


# --------------------------------------------------------------------------- #
# start
# --------------------------------------------------------------------------- #


class AgentStartFailed(CliError):
    """The agent exited right after `vl agent start` spawned it."""

    def __init__(self, org: str, returncode: int, log: Path) -> None:
        tail = nodeops.read_tail(log)
        self.reason = f"exited right after starting ({nodeops.describe_exit(returncode)})"
        lines = [f"The agent for {org} {self.reason}."]
        hint = EXIT_HINTS.get(returncode)
        if hint:
            lines.append(hint)
        lines.append("---- Log tail ----")
        lines.extend(tail if tail is not None else [f"(log file {log} not found)"])
        super().__init__("\n".join(escape(line) for line in lines))


@app.command("start")
def start(
    org: Annotated[str, typer.Argument(help="Organization name.")],
    env: EnvOption = None,
    as_: AsOption = None,
) -> None:
    """Start the org's agent, detached. Needs ORG_ADMIN of the org, or PLATFORM_ADMIN."""
    with report_errors():
        environment = store.get_environment(env)
        require_operator(environment, as_, org)
        org_name = _org_name(environment, org)
        agent = store.get_agent(environment.name, org_name)
        label = agent_label(agent)
        if is_running(agent, label):
            note(f"The agent for {org_name} is already running (pid {agent.pid}).")
            return
        if not kafka_installed():
            raise CliError(escape(NOT_INSTALLED.format(python=sys.executable)))

        log = log_path(org_name)
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "w") as log_file:
            # Its own session, as for nodes: it outlives this command, and a Ctrl-C
            # here never reaches it. The default close_fds: it inherits nothing.
            child = subprocess.Popen(
                agent_argv(environment, label),
                stdout=log_file,
                stderr=log_file,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
        agent = store.set_agent_pid(agent, child.pid)
        time.sleep(START_CHECK_SECONDS)
        returncode = child.poll()
        if returncode is not None:
            store.set_agent_stopped(agent)
            raise AgentStartFailed(org_name, returncode, log)

    host = f", host {environment.node_host} (the environment's --node-host)" if environment.node_host else ""
    console.print(f"Started the agent for [bold]{org_name}[/bold] (pid {child.pid}{escape(host)}).")
    note(f"Log: {log}")
    note(f"Results: {agent_config.results_path(org_name)} (vl agent results {org_name})")


# --------------------------------------------------------------------------- #
# stop
# --------------------------------------------------------------------------- #


def _wait_gone(pid: int, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while True:
        if not proc.is_alive(pid):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(POLL_SECONDS)


def _signal(pid: int, sig: signal.Signals) -> None:
    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        pass


@app.command("stop")
def stop(
    org: Annotated[str, typer.Argument(help="Organization name.")],
    env: EnvOption = None,
    as_: AsOption = None,
) -> None:
    """Stop the org's agent (SIGTERM, then SIGKILL after 30 s). Its nodes keep running."""
    with report_errors():
        environment = store.get_environment(env)
        require_operator(environment, as_, org)
        org_name = _org_name(environment, org)
        agent = store.get_agent(environment.name, org_name)
        label = agent_label(agent)
        if agent.pid is None or not proc.is_agent_process(agent.pid, label):
            if agent.state == "RUNNING":
                store.set_agent_stopped(agent)
                note(f"The agent for {org_name} was not running (pid {agent.pid} is gone); recorded it as STOPPED.")
            else:
                note(f"The agent for {org_name} is not running.")
            return
        pid = agent.pid
        # Only the agent's own pid: its nodes run in sessions of their own.
        _signal(pid, signal.SIGTERM)
        if _wait_gone(pid, STOP_TERM_SECONDS):
            how = "stopped"
        else:
            warn(f"The agent for {org_name} (pid {pid}) did not stop within {STOP_TERM_SECONDS:g} s of "
                 f"SIGTERM; sending SIGKILL.")
            _signal(pid, signal.SIGKILL)
            if not _wait_gone(pid, STOP_KILL_SECONDS):
                raise CliError(f"The agent for {org_name} (pid {pid}) is still alive after SIGKILL.")
            how = "killed"
        store.set_agent_stopped(agent)
    console.print(f"The agent for [bold]{org_name}[/bold] {how} (pid {pid}). Its nodes were not touched.")


# --------------------------------------------------------------------------- #
# list
# --------------------------------------------------------------------------- #

NOT_REGISTERED = "UNREGISTERED"


def _fetch_cp_agents(environment: store.Environment, caller: store.Identity,
                     org_id: str | None) -> list[dict[str, Any]]:
    """``GET /v1/agents`` (one page, its maximum of 500), as the caller."""
    params: dict[str, Any] = {"limit": 500}
    if org_id is not None:
        params["orgId"] = org_id
    body: dict[str, Any] = auth.authed_call(
        caller, environment.idp_base_url, lambda c: c.get("/v1/agents", params=params), environment.cp_base_url)
    return list(body.get("items") or [])


def _cp_view(environment: store.Environment, as_: str | None,
             org_id: str | None) -> tuple[list[dict[str, Any]] | None, str | None]:
    """The control plane's agents, or ``None`` and the one line saying why not."""
    try:
        caller = store.resolve_identity(environment.name, as_)
    except store.StoreError as exc:
        return None, f"Control plane columns not shown: no operator identity ({exc})."
    if caller.kind != "USER":
        return None, (f"Control plane columns not shown: '{caller.label}' is a service account, and the "
                      f"control plane lists agents only for a user (--as <label>).")
    try:
        return _fetch_cp_agents(environment, caller, org_id), None
    except api.ApiError as exc:
        if exc.status_code == 0 or exc.status_code >= 500:
            return None, f"Control plane columns not shown: it could not be reached ({exc})."
        return None, f"Control plane columns not shown: it refused the listing ({exc})."
    except auth.AuthError as exc:
        return None, f"Control plane columns not shown: no token for '{caller.label}' ({exc})."


def _mismatch(local_state: str, cp: dict[str, Any] | None) -> str | None:
    """The host-mismatch symptoms (cp-agent-commands-spec.md §3.3), or ``None``."""
    if cp is None:
        return None
    if local_state == "RUNNING" and cp.get("state") == "STALE":
        return (f"running here, but STALE at the control plane (last seen {cp.get('lastSeenAt') or '-'}): "
                f"its heartbeats are not arriving, or another host took its registration")
    if cp.get("nodeCount") == 0:
        return (f"no node of the org is registered with the agent's host {cp.get('host')}: the nodes report "
                f"another address (compare vl node list --remote; vl env update <env> --node-host sets one)")
    return None


def _table(title: str, rows: list[dict[str, Any]], attention: list[bool]) -> Table:
    table = Table(title=title)
    for column in rows[0]:
        table.add_column(column)
    for row, flagged in zip(rows, attention):
        table.add_row(*("" if v is None else escape(str(v)) for v in row.values()),
                      style="bold yellow" if flagged else None)
    return table


@app.command("list")
def list_(
    org: Annotated[str | None, typer.Argument(help="Only this org's agent.")] = None,
    remote: Annotated[
        bool, typer.Option("--remote", help="Every agent the control plane shows you, on any host.")
    ] = False,
    env: EnvOption = None,
    as_: AsOption = None,
) -> None:
    """List this host's agents, with the control plane's view of each."""
    with report_errors():
        environment = store.get_environment(env)
        organization = store.get_organization(environment.name, org) if org is not None else None
        org_id = organization.server_org_id if organization is not None else None
        if remote:
            _list_remote(environment, as_, org_id, org)
            return

        agents = [a for a in store.list_agents(environment.name)
                  if organization is None or a.org_name == organization.name]
        cp_rows, why = _cp_view(environment, as_, org_id) if agents else (None, None)
        server_ids = {o.name: o.server_org_id for o in store.list_organizations(environment.name)}
        rows: list[dict[str, Any]] = []
        flags: list[str | None] = []
        for agent in agents:
            label = agent_label(agent)
            if agent.state == "RUNNING" and not proc.is_agent_process(agent.pid, label):
                agent = store.set_agent_stopped_if_unchanged(agent)  # its process is gone
            cp = None
            if cp_rows is not None:
                cp = next((r for r in cp_rows
                           if r.get("orgId") == server_ids.get(agent.org_name) and r.get("name") == label), None)
            row: dict[str, Any] = {
                "org": agent.org_name,
                "agent": label,
                "state": agent.state,
                "pid": agent.pid,
                "cp_state": "-" if cp_rows is None else (cp.get("state") if cp else NOT_REGISTERED),
                "host": "-" if cp is None else cp.get("host"),
                "nodes": "-" if cp is None else cp.get("nodeCount"),
                "last_seen": "-" if cp is None else (cp.get("lastSeenAt") or "never"),
            }
            rows.append(row)
            flags.append(_mismatch(agent.state, cp))

    title = f"Agents on this host ({environment.name})"
    if is_json():
        render([{**r, "attention": f} for r, f in zip(rows, flags)], title=title)
        return
    if not rows:
        note("No agent on this host" + (f" for {org}" if org else "") +
             f" in {environment.name}. Create one with: vl agent create <org>")
        return
    console.print(_table(title, rows, [f is not None for f in flags]))
    if why:
        note(escape(why))
    for row, flag in zip(rows, flags):
        if flag:
            warn(f"{row['org']}: {flag}.")


def _list_remote(environment: store.Environment, as_: str | None, org_id: str | None, org: str | None) -> None:
    caller = store.resolve_identity(environment.name, as_)
    items = _fetch_cp_agents(environment, caller, org_id)
    org_name = org_name_resolver(environment)
    rows = [
        {
            "org": org_name(str(item.get("orgId") or "")),
            "agent": item.get("name"),
            "host": item.get("host"),
            "cp_state": item.get("state"),
            "nodes": item.get("nodeCount"),
            "last_seen": item.get("lastSeenAt") or "never",
            "version": item.get("agentVersion"),
            "id": item.get("id"),
        }
        for item in items
    ]
    title = "Agents (control plane)" + (f" in {org}" if org else "")
    if is_json() or not rows:
        render(rows, title=title)
        return
    console.print(_table(title, rows, [r["nodes"] == 0 for r in rows]))


# --------------------------------------------------------------------------- #
# results
# --------------------------------------------------------------------------- #

# Columns on a terminal, in order, with their widths (§6); DETAIL takes the rest.
COLUMNS: tuple[tuple[str, int], ...] = (
    ("COMMAND", 16), ("NODE", 14), ("OUTCOME", 15), ("TIME", 24), ("ID", 36),
)
DETAIL_MIN_WIDTH = 150
DEFAULT_LIMIT = 20
FOLLOW_POLL_SECONDS = 0.5


def is_terminal() -> bool:
    return sys.stdout.isatty()


def _cells(line: resultslog.Line) -> list[str]:
    return [line.get("command"), f"{line.get('org')}/{line.get('node')}", line.get("outcome"),
            line.get("time"), line.get("id")]


def _result_agent(environment: store.Environment, org: str | None) -> str:
    """The org whose results to read: the one named, or this host's only agent."""
    if org is not None:
        try:
            return _org_name(environment, org)
        except store.OrganizationNotFoundError:
            return org  # the files can outlive the cache
    agents = store.list_agents(environment.name)
    if not agents:
        raise CliError(f"No agent on this host in environment {environment.name!r}. "
                       f"Create one with: vl agent create <org>")
    if len(agents) > 1:
        names = ", ".join(a.org_name for a in agents)
        raise CliError(f"More than one agent on this host: {names}. Name one: vl agent results "
                       f"{agents[0].org_name}")
    return agents[0].org_name


def _print_table(org: str, lines: list[resultslog.Line], details: bool) -> None:
    width = console.width
    show_detail = details or width >= DETAIL_MIN_WIDTH
    table = Table(title=f"Agent results: {org} (newest first)", box=box.SQUARE)
    fixed = width >= DETAIL_MIN_WIDTH or not show_detail
    for name, w in COLUMNS:
        if fixed:
            # Fold rather than cut with an ellipsis if the terminal is narrower still:
            # an id that loses its end cannot be used with vl command show.
            table.add_column(name, width=w, overflow="fold")
        else:
            table.add_column(name, overflow="fold")
    if show_detail:
        table.add_column("DETAIL", overflow="fold", ratio=1)
    for line in lines:
        cells = _cells(line) + ([line.get("detail")] if show_detail else [])
        table.add_row(*(escape(c) for c in cells))
    console.print(table)
    if not show_detail:
        note(f"Terminal too narrow for DETAIL (need {DETAIL_MIN_WIDTH}, have {width}): widen it, or use --details")


def _stream_line(line: resultslog.Line) -> str:
    cells = _cells(line)
    return " ".join(c.ljust(w) for c, (_, w) in zip(cells, COLUMNS)) + " " + line.get("detail")


def _stream_header() -> str:
    return " ".join(n.ljust(w) for n, w in COLUMNS) + " DETAIL"


def _emit(text: str) -> None:
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def _follow(path: Path, limit: int, terminal: bool) -> None:
    follower = resultslog.Follower(path)
    current = follower.open_at_end()
    backlog = list(reversed(resultslog.newest_first(path, limit, current=current)))
    def show(line: resultslog.Line) -> None:
        _emit(_stream_line(line) if terminal else line.raw)

    _emit(_stream_header() if terminal else resultslog.HEADER_LINE.rstrip("\n"))
    for line in backlog:
        show(line)
    try:
        while True:
            for line in follower.poll():
                show(line)
            time.sleep(FOLLOW_POLL_SECONDS)
    finally:
        follower.close()


@app.command("results")
def results(
    org: Annotated[str | None, typer.Argument(help="The org (default: this host's only agent).")] = None,
    limit: Annotated[
        int | None, typer.Option("--limit", min=1, help=f"How many results, newest first (default {DEFAULT_LIMIT}).")
    ] = None,
    all_: Annotated[
        bool, typer.Option("--all", help="Every retained result, newest first (for less and grep).")
    ] = False,
    follow: Annotated[
        bool, typer.Option("--follow", help="The last results in time order, then new ones as they come, "
                                            "until Ctrl-C.")
    ] = False,
    details: Annotated[
        bool, typer.Option("--details", help="Show DETAIL even on a narrow terminal, wrapping cells.")
    ] = False,
    env: EnvOption = None,
) -> None:
    """Show the results the org's agent wrote (data/results.log), newest first."""
    if all_ and (limit is not None or follow):
        raise typer.BadParameter("--all cannot be combined with --limit or --follow", param_hint="--all")
    with report_errors():
        environment = store.get_environment(env)
        org_name = _result_agent(environment, org)
        path = agent_config.results_path(org_name)
        if org is not None and not resultslog.result_files(path):
            try:
                store.get_agent(environment.name, org_name)
            except store.AgentNotFoundError as exc:
                raise CliError(f"No agent for org {org_name!r} on this host in environment "
                               f"{environment.name!r}, and no results at {path}. Create one with: "
                               f"vl agent create {org_name}") from exc
    terminal = is_terminal()
    if follow:
        try:
            _follow(path, limit or DEFAULT_LIMIT, terminal)
        except KeyboardInterrupt:
            pass
        return

    lines = resultslog.newest_first(path, None if all_ else (limit or DEFAULT_LIMIT))
    if is_json():
        render([dict(zip(resultslog.HEADER, line.fields)) for line in lines], title=f"Agent results: {org_name}")
    elif not terminal:
        _emit(resultslog.HEADER_LINE.rstrip("\n"))
        for line in lines:
            _emit(line.raw)
    elif not lines:
        note(f"No results yet for {org_name} ({path}).")
    else:
        _print_table(org_name, lines, details)
