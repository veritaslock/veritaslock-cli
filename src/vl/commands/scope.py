"""Which nodes a verb is about, and where they are (vl-agent-cli-spec.md §2).

One function, :func:`resolve_scope`, shared by `vl network|org start|stop|reset`
and by `vl node list --remote|--all`:

1. **The control plane's nodes in scope:** ``GET /v1/nodes`` with the operator's
   identity, paged to the end (the control plane scopes it by caller). This host's
   ``NEW`` nodes are added from the store: the control plane does not know them yet.
2. **A node with a row in this store is local** unless all three hold: (a) the
   control plane has a registration for it from a host other than this host's
   address; (b) its row has no live process here (``nodeops.is_our_process``);
   (c) the control plane's state for it is not ``DOWN``. The control plane's
   states are ``UP``, ``STANDBY`` (running, catching up before it votes) and
   ``DOWN`` (NodeState); ``UP``, ``STANDBY`` and any state it does not name count
   as "may be running". Only then is the row a copy of a node that may run
   elsewhere, and remote, so `vl` never starts a second copy of it. A node this
   host runs, or a stopped one, registered from an old address of this host (a
   ``--node-host`` change, a new network) stays local: it re-registers from the
   new address when it is started. A node with no row here is remote.
3. **This host's address** is the environment's ``node_host`` when it is set, else
   ``verilock --print-host`` from the shared install. Hosts are compared trimmed and
   lower-cased, as the control plane matches nodes to agents. With neither (no
   ``node_host``, and no ``verilock`` that answers ``--print-host``), a registered
   host counts as this host's when it is this machine's name or one of its
   interface addresses, the check `vl node list` already makes.

If the control plane cannot be reached the verb must not guess: :class:`ScopeUnreachable`
(exit 4). Any other refusal (``401``, ``403``) is raised as the control plane's error.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from vl.agent import config as agent_config
from vl.agent import hostcheck
from vl.commands._shared import CliError
from vl.lib import api, auth, host, nodeops, store

EXIT_UNREACHABLE = 4

# The control plane's NodeState for a node that is certainly not running.
CP_DOWN = "DOWN"

UNREACHABLE = ("Cannot reach the control plane to find the nodes in scope; nothing was done. "
               "Use --local to act on this host's nodes only.")


class ScopeUnreachable(CliError):
    """The control plane could not be reached to resolve the scope (exit 4)."""

    def __init__(self, cause: api.ApiError) -> None:
        super().__init__(f"{UNREACHABLE} ({cause})")
        self.cause = cause


def normalize_host(value: str) -> str:
    """As the control plane compares hosts (AgentScopeResolver.normalizeHost)."""
    return value.strip().lower()


@dataclass
class ThisHost:
    """This host's address, worked out once per command."""

    address: str | None
    source: str  # "node_host", "print-host" or "interfaces"
    _local: set[str] | None = None

    def is_here(self, registered: str | None) -> bool:
        if registered is None or not registered.strip():
            return True  # no registration to disagree with
        if self.address is not None:
            return normalize_host(registered) == normalize_host(self.address)
        if self._local is None:
            self._local = host.local_identities()
        return host.is_this_machine(registered, self._local)


def this_host(environment: store.Environment,
              print_host: Callable[[Any], str] = hostcheck.print_host) -> ThisHost:
    """The environment's ``node_host``, else ``verilock --print-host``, else the interfaces."""
    if environment.node_host is not None:
        return ThisHost(environment.node_host, "node_host")
    binary = agent_config.default_verilock()
    if binary.is_file():
        try:
            return ThisHost(print_host(binary), "print-host")
        except hostcheck.PrintHostError:
            pass
    return ThisHost(None, "interfaces")


@dataclass(frozen=True)
class ScopeNode:
    org_id: str
    org_name: str  # the cached name, or the id for an org this store has never cached
    org_node: int
    row: store.Node | None  # this store's row for the node, if any
    cp: dict[str, Any] | None  # the control plane's row, if it has one
    local: bool
    moved: bool = False  # local although registered from another host

    @property
    def label(self) -> str:
        return f"{self.org_name}/{self.org_node}"

    @property
    def host(self) -> str | None:
        return None if self.cp is None else self.cp.get("host")

    @property
    def is_new(self) -> bool:
        """A local node that was never started: the control plane cannot reach it."""
        return self.row is not None and self.row.node_state == "NEW"


@dataclass
class Scope:
    kind: str  # NETWORK, ORG or NODE
    organization: store.Organization | None
    org_node: int | None
    nodes: list[ScopeNode] = field(default_factory=list)
    here: ThisHost | None = None

    @property
    def local(self) -> list[ScopeNode]:
        return [n for n in self.nodes if n.local]

    @property
    def remote(self) -> list[ScopeNode]:
        return [n for n in self.nodes if not n.local]

    @property
    def moved(self) -> list[ScopeNode]:
        return [n for n in self.nodes if n.moved]

    def moved_note(self) -> str | None:
        """The one line for local nodes last registered from another host, if any."""
        if not self.moved:
            return None
        by_host: dict[str, list[str]] = {}
        for n in self.moved:
            by_host.setdefault(normalize_host(n.host or ""), []).append(n.label)
        where = "; ".join(f"{', '.join(labels)} last registered from {h}" for h, labels in by_host.items())
        here = self.here.address if self.here is not None and self.here.address else "this machine"
        return f"{where}; this host is {here}: treated as local, they re-register when started."

    def why_remote(self) -> str:
        """The one line `vl` prints when it goes through the control plane."""
        names = ", ".join(n.label for n in self.remote)
        what = (f"node {self.organization.name}/{self.org_node}" if self.kind == "NODE" and self.organization
                else f"org {self.organization.name}" if self.organization
                else "the network")
        return (f"Not every node of {what} is on this machine (remote: {names}): "
                f"sending the command through the control plane.")


def fetch_cp_nodes(environment: store.Environment, caller: store.Identity) -> list[dict[str, Any]]:
    """Every node the control plane shows ``caller`` (``GET /v1/nodes``), paged to the end.

    Raises :class:`ScopeUnreachable` when it cannot be reached (a transport failure
    or a ``5xx``); any other error as it is.
    """
    def page_all(client: api.AppClient) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            body: dict[str, Any] = client.get("/v1/nodes", params={"cursor": cursor} if cursor else None)
            items.extend(body.get("items") or [])
            cursor = body.get("nextCursor")
            if not cursor:
                return items

    try:
        return auth.authed_call(caller, environment.idp_base_url, page_all, environment.cp_base_url)
    except api.ApiError as exc:
        if exc.status_code == 0 or exc.status_code >= 500:
            raise ScopeUnreachable(exc) from exc
        raise


def locality(row: store.Node, cp: dict[str, Any] | None, here: ThisHost) -> tuple[bool, bool]:
    """``(local, moved)`` for a node with a row in this store (the rule in the module doc).

    ``moved`` is a local node whose registration names another host.
    """
    if cp is None or here.is_here(cp.get("host")):
        return True, False
    if nodeops.is_our_process(row):
        return True, True
    if str(cp.get("state")) == CP_DOWN:
        return True, True
    return False, False


def resolve_scope(
    environment: store.Environment,
    caller: store.Identity,
    *,
    organization: store.Organization | None = None,
    org_node: int | None = None,
    rows: list[store.Node] | None = None,
    here: ThisHost | None = None,
) -> Scope:
    """The nodes of the network (no org), an org, or one node, and which are local.

    ``rows`` are this store's rows to consider (default: all of the scope's);
    `vl node list` passes the ones the caller may see.
    """
    kind = "NETWORK" if organization is None else ("ORG" if org_node is None else "NODE")
    cp_rows = fetch_cp_nodes(environment, caller)
    if rows is None:
        rows = store.get_nodes(environment, organization) if organization else store.get_nodes(environment)
    if organization is not None:
        rows = [r for r in rows if r.org_name == organization.name]
        cp_rows = [r for r in cp_rows if str(r.get("orgId")) == organization.server_org_id]
    if org_node is not None:
        rows = [r for r in rows if r.org_node == org_node]
        cp_rows = [r for r in cp_rows if r.get("orgNode") == org_node]
    cp_rows = [r for r in cp_rows if r.get("orgId") and isinstance(r.get("orgNode"), int)]

    orgs = store.list_organizations(environment.name)
    name_of = {o.server_org_id: o.name for o in orgs}
    id_of = {o.name: o.server_org_id for o in orgs}
    here = here if here is not None else this_host(environment)
    cp_by_key = {(str(r["orgId"]), int(r["orgNode"])): r for r in cp_rows}

    nodes: list[ScopeNode] = []
    seen: set[tuple[str, int]] = set()
    for row in rows:
        org_id = id_of.get(row.org_name, row.org_name)
        key = (org_id, row.org_node)
        seen.add(key)
        cp = cp_by_key.get(key)
        local, moved = locality(row, cp, here)
        nodes.append(ScopeNode(org_id, row.org_name, row.org_node, row, cp, local, moved))
    for key, cp in cp_by_key.items():
        if key not in seen:
            nodes.append(ScopeNode(key[0], name_of.get(key[0], key[0]), key[1], None, cp, False))
    nodes.sort(key=lambda n: (n.org_name, n.org_node))
    return Scope(kind, organization, org_node, nodes, here)
