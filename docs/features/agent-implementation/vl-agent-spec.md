# VeritasLock Host Agent (`vl-agent`)

**Status:** Draft for review. Nothing is built.
**Spec 2 of the series.** Spec 1 is `cp-agent-commands-spec.md` (the control plane's half: tables, topics, schemas, REST, roles). To follow: the `vl` changes, then packaging (container and `systemd`).
**Companion documents:** `vl-node-spec.md` (the node verbs, `store.db`, the `/proc` liveness checks), `vl-org-phase2-spec.md` §8 and `vl-network-spec.md` (today's org and network orchestration), `vl-service-account-spec.md` (service accounts and token acquisition), `verilock-node-startup-spec.md` §2 (the registration pattern the agent copies).

---

## 1. What the agent is

A small Python program, `vl-agent`, in the `veritaslock-cli` repo. It shares `vl`'s code. It does one job: when the control plane publishes a command to start, stop or reset nodes, it performs that command on the nodes of its own org that live on its own host, and reports what happened.

It is **not** a supervisor. It does not watch nodes, restart dead ones or keep a desired state. Those are future items, and nothing here prevents them.

One agent serves exactly one org. A host that runs several orgs (a development machine) runs one agent per org. A production host runs one.

---

## 2. Identity and startup

### 2.1 Identity

The agent is a `NODE_AGENT` service account (`cp-agent-commands-spec.md` §5.3), created with `vl service-account add <name> --role NODE_AGENT --org <org>`. That stores it in `store.db` like any other service account. The agent is started with `--as <label>` and obtains its JWT the way every `vl` call as a service account does today (`vl-service-account-spec.md` §6), so it needs no new authentication code. It re-acquires the token on a `401`.

### 2.2 Directory

```
$HOME/orgs/<org>/agent/
    identity/agent.id        the id the control plane assigned (cached, like a node's node.id)
    agent.properties         optional overrides (§3)
    results.log              the results table (§4.4), and nothing else; rotated to results.log.1, .2, …
```

The agent's **general log** (startup, heartbeats, warnings, errors, debug) goes to stdout. Whoever starts it redirects it, the way `vl node start` does for nodes (`$HOME/tmp/<org>-agent.out`). It never goes into `results.log`.

### 2.3 Startup sequence

1. Read the configuration (§3) and the cached `agent.id`, if any.
2. Authenticate as the service account.
3. Register: `POST /v1/agents` with `name`, `host`, `agentVersion` and the cached `agentId`. Write the returned id to `identity/agent.id`.
   - `409 AGENT_ID_MISMATCH` (the cached id is not this service account's): exit with a clear message. The identity directory was probably copied from another agent.
   - `409 AGENT_HOST_TAKEN` (another agent of the org is already registered for this host): exit with a clear message.
   - Control plane unreachable: retry with backoff for about a minute, then exit 69.
4. Start the heartbeat (§5).
5. Start consuming commands (§4). The agent consumes only after it has registered, so a command can never reach an agent the control plane does not know.

### 2.4 Exit codes

| Code | Meaning |
|---|---|
| 0 | Stopped by `SIGTERM` or `SIGINT` |
| 1 | Startup failed for another reason (the message says why) |
| 69 | The control plane or Kafka was unreachable after the retries |
| 77 | Authentication was refused, or the account is not a `NODE_AGENT` |
| 78 | Configuration or identity is invalid or missing |

`vl` can print a hint for each, as it does for node exit codes.

---

## 3. Configuration

Command line: `vl-agent --as <label> [--env <env>] [--host <address>] [--verilock <path>] [--log-level <level>]`. The org is not a parameter: it is the service account's own org, from its JWT.

| Setting | Source |
|---|---|
| Kafka bootstrap servers, Schema Registry URL, the Kafka client overrides | The environment settings `vl` already keeps (the same ones it renders into a node's configuration), including `env_kafka_property_override`. Turning on broker authentication later needs no agent change. |
| Control plane URL | The environment, as for every `vl` call. |
| `host` | `--host`, else the output of `verilock --print-host`, run from the bundle's binary or `--verilock <path>` (§8, open item 2). Must be identical to what the org's nodes report. |
| Agent name | `--name`, else the service account's display name. |
| Heartbeat interval | `agent.heartbeat.interval-seconds`, default 10. |
| Command expiry slack | `agent.command.skew-warn-seconds`, default 5. |
| Results file | `agent.results.file`, default `$HOME/orgs/<org>/agent/results.log`. |
| Results file rotation | `agent.results.max-bytes` (default 10485760) and `agent.results.keep` (default 5 files). |
| Whose results | `agent.results.all-orgs`, default `false`: only the agent's own org. |

---

## 4. Handling commands

### 4.1 Consuming

- Topic `veritaslock-agent-commands`, consumer group `veritaslock-agent-<agentId>` (every agent gets every command), `enable.auto.commit=false`, `auto.offset.reset=latest`. Avro, through the Schema Registry.
- **One consumer thread, one command at a time, in order.** A `STOP` followed by a `START` must not swap.
- A message is committed after it has been handled. A message that cannot be decoded is logged at ERROR and skipped (committed), never retried.

### 4.2 Deciding what to do with a command

In this order:

1. **Duplicate.** If the `correlationId` was already handled (an in-memory set, kept for an hour), drop it silently. Commands are also idempotent, so a repeat after a restart changes nothing.
2. **Not mine.** Every agent receives every command, including those for other orgs and other hosts, so this is the normal way a command is ignored. Keep only the `targets` whose `agentId` is this agent's. If none, ignore the command silently, with no log line and no result. Each target carries the `agentId`, and not only the org and node, so that the control plane (from the node's registered `host`) is the one place that decides which agent owns a node. Otherwise two hosts that each hold a `store.db` row for the same `(org, org_node)`, after a node was moved or copied, would both act on it, and whichever registered first could claim the node for the wrong host.
3. **Inconsistent target.** A target that names this agent but another org is refused with a WARN and not acted on. The control plane never produces one (it matches agent and node by org and `host`), so this is a defense against a bad or forged message, not part of ordinary filtering. It matters on a shared development host, where `store.db` also holds other orgs' nodes: the agent must never look one up.
4. **Expired.** If `now > expiresAt`, publish `EXPIRED` for each of my targets and do nothing. If `issuedAt` is more than `agent.command.skew-warn-seconds` in the future, log a WARN about clock skew (hosts need NTP). **Expiry is checked once, at the start.** A long `STOP` that began in time is carried through to the end, even past `expiresAt`.
5. **Look up each target** in `store.db` by `(org, org_node)`. A node that is not on this host gets `FAILED` ("node not found on this host").

Then act on the targets **in the order listed**. The control plane lists them by org, then `org_node` ascending for `START` and `RESET` and descending for `STOP`, the order `vl org start|stop` uses today.

### 4.3 What each command does

The agent calls the same functions `vl node start`, `stop` and `reset` call, so there is one implementation of each. Three consequences:

- **No prompts.** The functions need non-interactive entry points (the equivalent of `--yes`). The command was authorized at the control plane.
- **No credentials of its own for node work.** `vl node start` and `stop` call no control plane or identity provider operation as an operator. `stop` checks the node's state with the **node's own** service account, as `vl org stop` does today, so the agent needs no extra permission.
- **`RESET` never stops a node, and needs no special mode to guarantee it.** The per-node function it calls, `vl node reset`, never stops anything: it refuses unless the control plane reports the node `DOWN` (one check, no poll, skipped for a node that has never been started) (`vl-node-spec.md` §5.4). `vl org reset` used to stop running nodes first; it no longer does (`vl-org-phase2-spec.md` §8), so a reset means the same everywhere: every node it touches must already be down. The agent acts per target, and the control plane has already required every target to be `DOWN`.

| Command | The agent does | Result outcomes |
|---|---|---|
| `START` | `vl node start`: pre-check, render the configuration, spawn detached, wait 1 s and check it is alive (`vl-node-spec.md` §5.2). | `STARTED` (the detail carries the pid); `ALREADY_RUNNING`; `FAILED` (the exit code and the same hint `vl` prints, for example 69 means "the control plane still has this node as `UP`, retry in about 30 s"). |
| `STOP` | `vl node stop`: signal the node, poll its state with a timeout (§5.3 of the node spec). | `STOPPED`; `ALREADY_STOPPED` (no pid on record, or a stale pid); `FAILED` (the timeout, with what it saw). |
| `RESET` | Refuse if the control plane does not report the node `DOWN` (the function's own check), or if its process is alive here (`FAILED`, "node process is running (pid N)"). Otherwise delete its `data` directory, and `snapshots` too if `options.includeSnapshots` is `"true"`. | `RESET`; `FAILED`. |

`STARTED` means the process is alive after a second. It does not mean the node has reached `UP`: that shows in the control plane's state.

### 4.4 Results

After **each target** (not at the end of the command), the agent publishes `AgentCommandResultV1` to `veritaslock-agent-results`: key `correlationId`, `outcome`, `detail`, `exitCode` where there is one, `completedAt`. `acks=all`. If the publish fails, it is retried a few times and then logged at ERROR; the control plane will show `NO_ANSWER`, which is true.

The agent also **consumes** `veritaslock-agent-results` (its own consumer group, `veritaslock-agent-results-<agentId>`, `latest`) and writes every result it reads, its own included, as one row of a table in a **dedicated file**, `results.log` (§2.2). That file holds results and nothing else: the agent's general messages go to stdout and never into it. It is written through its own logger with its own handler, so it can be tailed, read or loaded as it is. Someone at any host of the org sees the outcome of a remote operation by looking at that one file.

Rows are fixed-width, columns separated by two spaces, one line per result, in the order consumed. The free-text detail comes last and is never wrapped (a newline in it becomes a space). A header is written at the top of the file and again at the top of each rotated file.

```
TIME                      COMMAND                               TYPE   NODE      AGENT    HOST         OUTCOME          EXIT  DETAIL
2026-10-27T09:14:06.412Z  7f3a1c2e-5b6d-7a10-8e21-3c9d4f5a6b7c  START  globo/3   dev-box  192.168.8.2  STARTED          -     pid 48213
2026-10-27T09:14:07.093Z  7f3a1c2e-5b6d-7a10-8e21-3c9d4f5a6b7c  START  globo/4   lab-2    192.168.8.9  FAILED           69    the control plane still has this node as UP; retry in about 30 seconds
```

| Column | Content |
|---|---|
| `TIME` | `completedAt`, UTC, to the millisecond (24 wide) |
| `COMMAND` | The full `correlationId` (36 wide), so a row can be matched to `vl command show <id>` |
| `TYPE` | `START`, `STOP` or `RESET` |
| `NODE` | `<org>/<org_node>`: the org's name where the agent knows it (from `store.db`'s organization cache), its id otherwise |
| `AGENT`, `HOST` | The reporting agent's name and `host`, carried in the result message, so reading the file needs no lookup |
| `OUTCOME` | One of the outcomes in `cp-agent-commands-spec.md` §3.4 |
| `EXIT` | The exit code, or `-` |
| `DETAIL` | Free text, last, not cut |

A value longer than its column is cut with `…`, except `DETAIL`. By default only results for the agent's own org are written (`agent.results.all-orgs=false`). The file rotates at `agent.results.max-bytes` and keeps `agent.results.keep` files.

---

## 5. Heartbeat

A separate thread sends `POST /v1/agents/{agentId}/heartbeat` every 10 seconds with the agent's JWT (`cp-agent-commands-spec.md` §5.3).

| Answer | The agent |
|---|---|
| `204` | Carries on. |
| `404` (the control plane does not know it) | Registers again, then carries on. |
| `401` | Gets a new token and retries. |
| `409 AGENT_ID_MISMATCH` | Logs it and exits 78. |
| No answer | Logs a WARN and keeps trying. A control plane outage must not stop an agent from carrying out commands already published. |

---

## 6. Sharing `store.db` with `vl` and other agents

`vl` and one or more agents on a host write the same `store.db`: a node's `pid` and `state` when started and stopped, the repair that `vl node list` makes when it finds a dead pid, and port allocation. SQLite makes the file safe; the risk is the application's read-then-write races:

- Two actors both read `STOPPED` and both start the node, and the row then holds the pid of whichever wrote last.
- A reader repairs a row `RUNNING` to `STOPPED` while a starter is part-way through.
- Two `vl node create` calls allocate the same port.

Requirements, built into `vl`'s code (so the agent gets them by sharing it):

1. **One code path** for start, stop and reset. No second copy in the agent.
2. **A per-node lock**: an advisory `flock` on `<node_root>/.lifecycle.lock`, held for the whole start, stop or reset, and by the repair. A human who finds it taken waits up to 10 seconds, then gets "another operation is in progress". The agent does the same and reports `FAILED` with that detail. The repair uses a try-lock and skips itself.
3. **Store settings**: WAL mode, a 10-second busy timeout, and `BEGIN IMMEDIATE` on every transaction that reads and then writes (state changes, repair, port allocation), with a unique constraint on the port.
4. **A node survives its agent.** Nodes are spawned in their own session (`start_new_session=True`), so an agent that exits or crashes leaves them running. A restarted agent does not need to find them again.

The agent must run as the OS user that owns `$HOME/orgs/...` and `store.db`, the same user that runs `vl`. The store must be on a local disk (WAL does not work on network file systems).

---

## 7. Running it

Foreground, logging to stdout, handling `SIGTERM` by finishing the node operation in hand and exiting without touching the nodes. That is all the agent needs to run any of these ways:

- **By hand or from `vl`** (a later verb, `vl agent start`, mirroring `vl node start`: detached, output to `$HOME/tmp/<org>-agent.out`).
- **As a `systemd` unit**, `User=` the owner of the store, `Restart=on-failure`. Supervising the agent this way is a future item.
- **In a container**: the same program, with the host's directory mounted for `$HOME/orgs`. The container needs a small init as PID 1 with the agent as its child, because the nodes are the agent's children in other sessions. Packaging is its own spec.

---

## 8. Open items

1. **Ordering across hosts and orgs. Decided: A, drop cross-org ordering.** Today `vl network start` starts orgs one at a time, GLOBO first, and `vl network stop` stops them in reverse, GLOBO last, with a pause between orgs and a wait for each node to be `DOWN` in the control plane (`vl-network-spec.md` §3, `vl-org-phase2-spec.md` §8). The reason is the leader: it must be in GLOBO, so the network cannot become viable until GLOBO is, and stopping GLOBO early costs the network its viability sooner than necessary. The delays between stops also let the protocol events be consumed while nodes go off line. A broadcast command cannot keep that order across hosts, because each agent acts at once. The owner's decision: accept that. The consequences:
   - **Start:** none that matters. The network becomes viable when GLOBO's nodes are, whatever the order.
   - **Stop:** the network may lose viability sooner than with the old order, and some `NodeDown` events may not reach a block before it does. They appear in the blocks built after the next start. Accepted.
   - **What is kept:** the order of nodes **within one agent's list** (ascending for `START` and `RESET`, descending for `STOP`), and the per-node wait for `DOWN` before the next `STOP`, because the agent runs the same functions as `vl org stop`. Protocol events are therefore still consumed between the stops of one org.
   - **A cheap improvement if it ever matters:** a per-target delay carried in the command, so the control plane can ask the GLOBO agent to wait before it starts a `STOP`. Not built now.
   - **For the `vl` spec:** what `vl network start|stop` does when agents are present (one command to the control plane, unordered) against when they are not (today's local, ordered loops).
2. **How the agent gets the same `host` as the nodes. Decided in principle: C.** The control plane matches a node to its agent by `host`, so the two must agree exactly. `verilock` detects its own address (`NodeIdentity::getHostName()`, read from the code), once at startup, before it registers:
   - it walks `getifaddrs()` in the order the OS returns it, IPv4 only, skipping loopback, interfaces that are not up and running, and virtual ones by name prefix (`docker`, `veth`, `br-`, `tun`, `wg`, `virbr`);
   - it takes the first **wired** candidate (no `/sys/class/net/<name>/wireless`), else the first candidate of any kind;
   - if nothing qualifies, it falls back to the OS hostname.

   Consequences: IPv6 is ignored; a machine with no usable interface reports its name, not an IP; virtual adapters outside the prefix list (`tailscale0`, ZeroTier, VMware, VirtualBox, `cni`, `flannel`) can be picked, an existing risk for the nodes too; and the value is the address at node startup, so a DHCP change leaves node and agent disagreeing until the next restart (`nodeCount` on `GET /v1/agents` shows zero, so it is visible).

   Options: **A.** copy the algorithm in Python: needs a third-party package for `getifaddrs`, and any difference in interface order or the `/sys` check is a silent mismatch. **B.** a configured `host` that `vl` renders into every node's configuration, with `verilock` using it when present and detecting otherwise; exact and handles NAT and containers, but needs rendering as well as a node change. **C.** `verilock --print-host` runs the node's own detection and prints the address; the agent uses the output at startup. Exact by construction, no duplicated algorithm.

   **Recommendation: C**, with B later if an override is ever needed. The node change should be about 20 lines: make the detection a static function or a free function (it uses no members), handle `--print-host` at the very top of `main()`, before `AppCntx` is built (the `NodeIdentity` constructor registers with the control plane, so it must not be involved), print the address alone and exit 0; and split the selection into a pure function over a list of interface records so it can be tested without the machine's real network. To be confirmed by the node agent when it builds it, since `main()` and the headers were not read. The agent keeps `--host` for special cases, and takes the binary from the same bundle (next to `vl-agent`, or `--verilock <path>`). An older binary without the flag makes the agent exit 78 and ask for `--host`.
3. **Libraries.** The repo gains a Kafka client and an Avro and Schema Registry client. I recommend `confluent-kafka` (the same librdkafka as the node, with its Schema Registry Avro serializer), added with `uv`.
4. **Long commands and the single queue.** A long `STOP` of many nodes delays the next command, which may then expire and report `EXPIRED`. That is acceptable and visible. If it proves a nuisance, a second worker for other orgs' targets is a later option, but one agent serves one org, so it rarely arises.
5. **What this implies for `vl`** (the next spec): the non-interactive entry points (the `--yes` equivalents); the `flock`, WAL and `BEGIN IMMEDIATE` changes; `vl service-account add --role NODE_AGENT`; `vl agent start|stop|status`; the remote command verbs and `vl command show|watch`; `vl agent results [-f]` to show or follow `results.log`; the hint about the host's own `NEW` nodes after a remote command; and a decision on whether `vl node start|stop|reset` for a node on this host stay local while org and network commands go through the control plane.

---

## 9. Tests

- **Command handling, with fakes** for the node operations, the producer and the clock: duplicate dropped; not mine ignored; wrong org refused; expired gives `EXPIRED` for each of my targets; expiry checked only at the start; order of targets kept; an unknown node gives `FAILED`; a poison message is skipped and committed.
- **Outcome mapping**: each row of §4.3, including the exit-code hint for a failed `START`, and `ALREADY_RUNNING` and `ALREADY_STOPPED`.
- **`RESET`**: refused while a process is alive; never stops a node; removes `data` only, or `data` and `snapshots` with the option.
- **Concurrency**: two threads or processes starting the same node produce one `STARTED` and one `ALREADY_RUNNING` or a busy `FAILED`, never two live processes; repair does not clobber a start in progress; two `create` calls never share a port.
- **Heartbeat**: each answer in §5, including re-registration on `404`.
- **Startup**: each outcome of §2.3, and the exit codes.
- **Results file**: only table rows ever land in it (no general log line); the header at the top of the file and of each rotated file; a multi-line detail on one line; own org only by default, all orgs with the setting; rotation at the size limit keeps the configured number of files; an unknown org or node name falls back to the id; a value longer than its column is cut, the detail is not.
- **Node survival**: stopping the agent leaves the nodes running.
- **Integration**, against a real Kafka and a stub control plane: a command in, results out, in order.
