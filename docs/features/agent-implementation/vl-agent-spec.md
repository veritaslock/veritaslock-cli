# VeritasLock Node Agent (`vl-agent`)

**Status:** Design settled after the owner's review. Not built; adjustments are expected once it is.
**Spec 2 of the series.** Spec 1 is `cp-agent-commands-spec.md` (the control plane's half: tables, topics, schemas, REST, roles). To follow: the `vl` changes, then packaging (container and `systemd`).
**Companion documents:** `vl-node-spec.md` (the node verbs, `store.db`, the `/proc` liveness checks), `vl-org-phase2-spec.md` §8 and `vl-network-spec.md` (today's org and network orchestration), `vl-service-account-spec.md` (service accounts and token acquisition), `verilock-node-startup-spec.md` §2 (the registration pattern the agent copies).

---

## 1. What the agent is

A small Python program, `vl-agent`, in the `veritaslock-cli` repo: the **node agent**, which serves the nodes of one org on one machine. It shares `vl`'s code. It does one job: when the control plane publishes a command to start, stop or reset nodes, it performs that command on the nodes of its own org that live on its own host, and reports what happened.

It is **not** a supervisor. It does not watch nodes, restart dead ones or keep a desired state. Those are future items, and nothing here prevents them.

One agent serves exactly one org. A host that runs several orgs (a development machine) runs one agent per org. A production host runs one.

---

## 2. Identity and startup

### 2.1 Identity

The agent is a `NODE_AGENT` service account (`cp-agent-commands-spec.md` §5.3), created with `vl svc-acct add <name> --role NODE_AGENT --org <org>`. That stores it in `store.db` like any other service account. The agent is started with `--as <label>` and obtains its JWT the way every `vl` call as a service account does today (`vl-service-account-spec.md` §6), so it needs no new authentication code. It re-acquires the token on a `401`.

### 2.2 Directory, results file and general log

```
$HOME/orgs/<org>/agent/
    agent.properties         optional overrides (§3)
    data/
        results.log          the results, one pipe-delimited row each (§4.4), and nothing else; rotated to results.log.1, .2, …
```

The agent stores nothing else. In particular it does **not** store its own id: registration is find-or-create keyed by the service account, so the control plane returns the same id every time (`cp-agent-commands-spec.md` §5.3), and the credentials are already in `store.db`.

**The agent writes `data/results.log` itself**, so nothing depends on how it was started: not a redirect, not a launcher, not a path some other command has to guess. The file is structured output, like the chain files in a node's `data/`, and it holds the results, pipe-delimited, and nothing else. Its location is fixed (`vl agent results` reads it), so it is not configurable.

**The general log** (startup, heartbeat warnings, errors, debug, and the progress text of the shared functions) goes to stdout and stderr as usual, and whoever starts the agent redirects them: `vl agent start` sends both to `$HOME/tmp/<org>-agent.out`, as `vl node start` does for nodes. Neither stream is ever written into `results.log`. Everything the agent writes is UTF-8 whatever the locale says. At startup it logs the path of the results file.

### 2.3 Startup sequence

1. Read the configuration (§3).
2. Authenticate as the service account.
3. Register: `POST /v1/agents` with `name`, `host` and `agentVersion`. Keep the returned `agentId` in memory for this run.
   - `409 AGENT_ALREADY_ACTIVE` (this service account is active from another host: its heartbeat is fresh there): exit with a clear message. The credentials were probably copied to a second machine, or the agent is still running elsewhere. An agent restarting on the same host is never refused.
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
| Kafka bootstrap servers, Schema Registry URL, control plane URL | The `environment` row `vl` already keeps (`kafka_bootstrap`, `schema_reg_url`, `cp_url`, `token_url`; `vl-env-spec.md` §3). `env_kafka_property_override` exists in the schema but nothing consumes it yet (`vl-node-spec.md` §3), so the agent would be its first consumer when broker authentication is introduced. |
| Control plane URL | The environment, as for every `vl` call. |
| `host` | `--host`, else the output of `verilock --print-host`, run from the bundle's binary (default `$HOME/.veritaslock/bin/verilock`, where `vl node start` copies nodes' binaries from) or `--verilock <path>` (§8, open item 2). Must be identical to what the org's nodes report. |
| Agent name | `--name`, else the service account's display name. |
| Heartbeat interval | `agent.heartbeat.interval-seconds`, default 10. |
| Command expiry slack | `agent.command.skew-warn-seconds`, default 5. |
| Whose results | `agent.results.all-orgs`, default `false`: only the agent's own org. |
| Results file rotation | `agent.results.max-bytes` (default 10485760) and `agent.results.keep` (default 5 files). The location is fixed, `data/results.log`. |
| Output encoding | UTF-8 for the results file and the console streams, set by the agent whatever the locale. |

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
| `STOP` | `vl org stop`'s per-node routine (`vl-org-phase2-spec.md` §8): `vl node stop` (local only: `SIGTERM`, up to 10 s, then `SIGKILL`; `vl-node-spec.md` §5.3), then a poll of the control plane, as the node's own service account, until it reports `DOWN` (20 × 0.25 s), with the same short pauses between nodes. | `STOPPED`; `ALREADY_STOPPED` (no pid on record, or a stale pid); `FAILED` ("still reported UP" after the poll, with what it saw). |
| `RESET` | Refuse if the control plane does not report the node `DOWN` (the function's own check), or if its process is alive here (`FAILED`, "node process is running (pid N)"). Otherwise delete its `data` directory, and `snapshots` too if `options.includeSnapshots` is `"true"`. | `RESET`; `FAILED`. |

`STARTED` means the process is alive after a second. It does not mean the node has reached `UP`: that shows in the control plane's state.

### 4.4 Results

After **each target** (not at the end of the command), the agent publishes `AgentCommandResult` to `veritaslock-agent-results`: key `correlationId`, `command` (copied from the command), `outcome`, `detail`, `exitCode` where there is one, `completedAt`. `acks=all`. If the publish fails, it is retried a few times and then logged at ERROR; the control plane will show `NO_ANSWER`, which is true.

**What the control plane does with a result** (`cp-agent-commands-spec.md` §6.3): it matches the result on `correlationId`, `orgId`, `orgNode` **and `agentId`**, where `agentId` is the id returned by `POST /v1/agents` (not the service account id); the agent reports only the targets that carry its own `agentId`. **The first result for a node wins**: the agent publishes exactly one result per target, the final one, and never a provisional one such as "starting" (resending the same result is harmless). A result is still applied when it arrives after `expiresAt` or after `waitUntil`, and a result from a deleted agent too; a result for a command that has been cleaned up, or for a `SKIPPED` row, is dropped. `detail` is cut to 16,383 characters (the agent keeps it far shorter). A result must be serialized with the registered `AgentCommandResult` schema (subject `veritaslock-agent-results-com.veritaslock.protocol.events.agents.v1.AgentCommandResult`), key `correlationId`; one that cannot be deserialized is skipped by the control plane with one ERROR line and lost, so the agent never publishes a malformed one.

The agent also **consumes** `veritaslock-agent-results` (its own consumer group, `veritaslock-agent-results-<agentId>`; it commits its position after each row it writes, so a restart resumes where it stopped and `latest` applies only to the very first start) and appends every result it reads, its own included, as **one pipe-delimited row** to `data/results.log` (§2.2). Someone at any host of the org sees the outcome of a remote operation with `vl agent results --follow`, or by tailing the file.

The file is plain text, UTF-8, with the fields separated by a pipe (`|`), so that `grep`, `awk -F'|'` or a script can use it as it is, a spreadsheet can import it with `|` as the delimiter, and `vl agent results` (`vl-agent-cli-spec.md` §6) does the formatting for people. The delimiter is a pipe and not a comma because `detail` is free text, and commas occur naturally in it.

```
time|id|command|org|node|agent|host|outcome|exit|detail
2026-10-27T09:14:06.412Z|7f3a1c2e-5b6d-7a10-8e21-3c9d4f5a6b7c|vl network start|globo|3|dev-box|192.168.8.2|STARTED||pid 48213
2026-10-27T09:14:07.093Z|7f3a1c2e-5b6d-7a10-8e21-3c9d4f5a6b7c|vl network start|globo|4|lab-2|192.168.8.9|FAILED|69|the control plane still has this node as UP; retry in about 30 seconds
2026-10-27T09:14:07.410Z|7f3a1c2e-5b6d-7a10-8e21-3c9d4f5a6b7c|vl network start|globo|5|lab-2|192.168.8.9|ALREADY_RUNNING||pid 31877
```

| Column | Content |
|---|---|
| `time` | `completedAt`, UTC, ISO 8601 to the millisecond |
| `id` | The `correlationId`: the id of the command, to use with `vl command show <id>` |
| `command` | The command as the person typed it (`vl network stop`, `vl org reset`), from the result message |
| `org` | The org's name where the agent knows it (from `store.db`'s organization cache), its id otherwise |
| `node` | The node number within the org |
| `agent` | The reporting agent's name, carried in the result message |
| `host` | The reporting agent's `host`, likewise |
| `outcome` | One of the outcomes in `cp-agent-commands-spec.md` §3.4 |
| `exit` | The exit code, or empty |
| `detail` | Free text, last |

Rules: a field containing a pipe, a double quote or a newline is quoted, with its quotes doubled (the quoting of RFC 4180, with `|` as the delimiter; Python's `csv` module with `delimiter='|'` reads and writes it), which almost never happens, since commas need no quoting; a newline in the detail becomes a space, so **one result is always one line**; the header is written once, at the top of a file that is new or has just rotated, and **not** again when the agent restarts; the file is opened in append mode and each row is written and flushed at once, so `tail -f` shows it immediately; the file rotates at `agent.results.max-bytes` and keeps `agent.results.keep` files, and nothing but the header and the rows is ever written to it. `vl` also reads this file, to wait for a command it has just issued (`vl-agent-cli-spec.md` §7.5), so the `id` column and the rule of one result per line are an interface and not a detail. **Design note:** this is the model, view and controller split in miniature. The file is the model (every result, on the local disk, queryable with `grep`, `sed` and `awk`, with no control plane call); `vl agent results` is the view (it only formats what the file holds); the agent and `vl`'s verbs are the controllers (they act and write or read the model). Keeping the three apart is why `vl` can wait for a command by reading the file. By default only results for the agent's own org are written (`agent.results.all-orgs=false`). **What the file means:** it is what this agent **heard**, not the complete record of what happened. It holds only the nodes that answered; a node that never answered (`NO_ANSWER`) or had no agent (`SKIPPED`) has no row in it, and a command that is still running looks like a short list. The complete picture, with the overall status and the counts, is the control plane's (`vl command show`).

---

### 4.5 Consumer health

The Kafka library can leave a consumer deaf without any error that the application sees: in a node it happened after an 18 s stall of the whole machine, the blocks consumer never recovered, the poll loop treated every timeout as "nothing new", and the node's heartbeat, which runs elsewhere, kept saying `UP`. The agent's heartbeat is a REST call independent of its consumers, so it has the same weakness, and a deaf agent would look `ALIVE` while its targets time out as `NO_ANSWER`. So:

- **Every error is visible.** The agent sets the library's error callback and logs each error (rate-limited), including `ALL_BROKERS_DOWN` and transport errors, and never treats them as an idle poll.
- **A liveness check on each consumer** (commands and results), every 30 s: if its position is behind the broker's high watermark for its partition and no message has been delivered for 60 s, the agent logs an ERROR, closes the consumer and creates a new one, resuming from the committed offset (the commands consumer starts at the end only on its very first start). If the same consumer stalls again within 5 minutes, the agent **exits with status 1** after logging why, so that a person or a service manager notices, and its heartbeat stops, so the control plane shows it `STALE` and not `ALIVE`.
- **The heartbeat says how the consumers are:** it carries `commandsConsumerLagSeconds` and `resultsConsumerLagSeconds` (how long each has been behind with nothing delivered, 0 when current), so that `vl agent list` can show a stalled consumer even before the agent gives up.

## 5. Heartbeat

A separate thread sends `POST /v1/agents/{agentId}/heartbeat` every 10 seconds with the agent's JWT (`cp-agent-commands-spec.md` §5.3).

| Answer | The agent |
|---|---|
| `204` | Carries on. |
| `404` (the control plane does not know it) | Registers again, then carries on. |
| `401` | Gets a new token and retries. |
| `409 AGENT_ID_MISMATCH` (should not happen: the agent uses the id it was given this run) | Logs it and exits 78. |
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
4. **A node survives its agent.** Nodes are meant to be spawned in their own session (`start_new_session=True`), so an agent that exits or crashes leaves them running, and a restarted agent does not need to find them again. **To be confirmed in the code** that `vl node start` really spawns this way (open item 6). A session is not enough under `systemd`: stopping a unit kills every process in its control group, so the unit needs `KillMode=process` (§7).

The agent must run as the OS user that owns `$HOME/orgs/...` and `store.db`, the same user that runs `vl`. The store must be on a local disk (WAL does not work on network file systems).

---

## 7. Running it

Foreground, with its general log on the console and its results in `data/results.log` (§2.2), handling `SIGTERM` by finishing the node operation in hand and exiting without touching the nodes. That is all the agent needs to run any of these ways:

- **By hand or from `vl`** (a later verb, `vl agent start`, mirroring `vl node start`: detached, stdout and stderr to `$HOME/tmp/<org>-agent.out`).
- **As a `systemd` unit**, `User=` the owner of the store, `Restart=on-failure`, **`KillMode=process`** (without it, stopping or restarting the unit kills every node the agent started, because `systemd` kills the whole control group and a new session does not leave it), `StandardOutput` and `StandardError` sent to a log file or the journal; the results file needs nothing. Supervising the agent this way is a future item.
- **In a container**: the same program with the host's directory mounted for `$HOME/orgs`, so `data/results.log` is on the host and survives the container. The container needs a small init as PID 1 with the agent as its child, because the nodes are the agent's children in other sessions. Packaging is its own spec.

---

## 8. Open items

1. **Ordering across hosts and orgs. Decided: A, drop cross-org ordering.** Today `vl network start` starts orgs one at a time, GLOBO first, and `vl network stop` stops them in reverse, GLOBO last, with a pause between orgs and a wait for each node to be `DOWN` in the control plane (`vl-network-spec.md` §3, `vl-org-phase2-spec.md` §8). The reason is the leader: it must be in GLOBO, so the network cannot become viable until GLOBO is, and stopping GLOBO early costs the network its viability sooner than necessary. The delays between stops also let the protocol events be consumed while nodes go off line. A broadcast command cannot keep that order across hosts, because each agent acts at once. The owner's decision: accept that. The consequences:
   - **Start:** none that matters. The network becomes viable when GLOBO's nodes are, whatever the order.
   - **Stop:** the network may lose viability sooner than with the old order, and some `NodeDown` events may not reach a block before it does. They appear in the blocks built after the next start. Accepted.
   - **What is kept:** the order of nodes **within one agent's list** (ascending for `START` and `RESET`, descending for `STOP`), and the per-node wait for `DOWN` before the next `STOP`, because the agent runs the same functions as `vl org stop`. Protocol events are therefore still consumed between the stops of one org.
   - **A cheap improvement if it ever matters:** a per-target delay carried in the command, so the control plane can ask the GLOBO agent to wait before it starts a `STOP`. Not built now.
   - **For the `vl` spec (decided, `vl-agent-cli-spec.md` §2):** a verb whose every node is on this host keeps today's ordered, synchronous local loop; as soon as one node in scope is on another host, the whole verb is one unordered command through the control plane.
2. **How the agent gets the same `host` as the nodes. Decided in principle: C.** The control plane matches a node to its agent by `host`, so the two must agree exactly. `verilock` detects its own address (`NodeIdentity::getHostName()`, read from the code), once at startup, before it registers:
   - it walks `getifaddrs()` in the order the OS returns it, IPv4 only, skipping loopback, interfaces that are not up and running, and virtual ones by name prefix (`docker`, `veth`, `br-`, `tun`, `wg`, `virbr`);
   - it takes the first **wired** candidate (no `/sys/class/net/<name>/wireless`), else the first candidate of any kind;
   - if nothing qualifies, it falls back to the OS hostname.

   Consequences: IPv6 is ignored; a machine with no usable interface reports its name, not an IP; virtual adapters outside the prefix list (`tailscale0`, ZeroTier, VMware, VirtualBox, `cni`, `flannel`) can be picked, an existing risk for the nodes too; and the value is the address at node startup, so a DHCP change leaves node and agent disagreeing until the next restart (`nodeCount` on `GET /v1/agents` shows zero, so it is visible).

   Options: **A.** copy the algorithm in Python: needs a third-party package for `getifaddrs`, and any difference in interface order or the `/sys` check is a silent mismatch. **B.** a configured `host` that `vl` renders into every node's configuration, with `verilock` using it when present and detecting otherwise; exact and handles NAT and containers, but needs rendering as well as a node change. **C.** `verilock --print-host` runs the node's own detection and prints the address; the agent uses the output at startup. Exact by construction, no duplicated algorithm.

   **Recommendation: C**, with B later if an override is ever needed. The node change should be about 20 lines: make the detection a static function or a free function (it uses no members), handle `--print-host` at the very top of `main()`, before `AppCntx` is built (the `NodeIdentity` constructor registers with the control plane, so it must not be involved), print the address alone and exit 0; and split the selection into a pure function over a list of interface records so it can be tested without the machine's real network. To be confirmed by whoever builds it in the node repo, since `main()` and the headers were not read. The agent keeps `--host` for special cases, and takes the binary from the same bundle (next to `vl-agent`, or `--verilock <path>`). An older binary without the flag makes the agent exit 78 and ask for `--host`.
3. **Libraries. Decided.** The repo gains a Kafka client and an Avro and Schema Registry client. I recommend `confluent-kafka` (the same librdkafka as the node, with its Schema Registry Avro serializer), added with `uv`, as an **optional dependency group** (`vl[agent]`), so that hosts which only use `vl` do not carry a Kafka client they never use. **Pin `confluent-kafka>=2.16.0` in the `vl[agent]` extra:** the wheels from 2.16.0 on bundle librdkafka 2.16.0, which fixes the re-bootstrap bug that left Kafka handles without their broker after a brief fence (node spec item 37); and log the bundled librdkafka version at startup (`confluent_kafka.libversion()`), refusing to start below 2.16.0.
4. **Long commands and the single queue. Accepted.** A long `STOP` of many nodes delays the next command, which may then expire and report `EXPIRED`. That is acceptable and visible. If it proves a nuisance, a second worker for other orgs' targets is a later option, but one agent serves one org, so it rarely arises.
5. **What this implies for `vl`** (the next spec): the non-interactive entry points (the `--yes` equivalents); the `flock`, WAL and `BEGIN IMMEDIATE` changes; `vl svc-acct add --role NODE_AGENT`; `vl agent create|start|stop|list|results`; the remote command verbs and `vl command show|watch`; `vl agent results [--limit <n>] [--all] [--follow]` to show or follow `data/results.log` (`vl-agent-cli-spec.md` §6); an output callback in the shared start, stop and reset functions, so the agent logs their progress text at the right level (it is no longer needed for correctness, now that results have their own file); the hint about the host's own `NEW` nodes after a remote command; and a decision on whether `vl node start|stop|reset` for a node on this host stay local while org and network commands go through the control plane.

6. **Verify that nodes survive their agent.** `vl-node-spec.md` §5.2 says the node is spawned with `subprocess.Popen(..., start_new_session=True)`, in its own session, so an agent that exits or crashes leaves its nodes running. Confirm that the code matches the spec and that nothing ties the child's lifetime to the caller (no death signal set on the child), as the first step of spec 3's build. `systemd` still needs `KillMode=process` (§7).

---

7. **Host drift. Recommended.** The host an agent registers with comes from `verilock --print-host` (`node-study-lifecycle-spec.md` item 41), which picks the wired address while that interface is `RUNNING` and otherwise the wireless one or the hostname. If the network changes after the agent started (a cable unplugged when a node restarts), a node can register a different host than its agent, and every command then skips that node with `NO_AGENT` (`vl agent list` shows `nodeCount` 0). The agent therefore re-runs `--print-host` every 60 seconds; if the value, trimmed and lower-cased, differs from the host it registered with, it logs an ERROR naming both and exits with its own status (70, `host changed`) so that a person or a service manager restarts it; the restart re-registers with the new host, and the control plane refuses a registration from a different host with `AGENT_ALREADY_ACTIVE` while the old heartbeat is fresh, so the startup waits `agent.heartbeat.stale-seconds` and retries (to settle when the startup sequence is built). An agent never changes its host silently.

8. **The contract of the shared operations (built and reviewed, `agent-foundations`).** The agent imports only from `vl.lib` (`nodeops`, `nodelock`, `store`, `auth`), never from `vl.commands`. `start_node`, `stop_node`, `reset_node(..., include_snapshots=)` and `stop_nodes_to_down(environment, nodes, emit)` take `emit(text)` (the agent passes its logger) and `lock_wait` (default 10 seconds), never prompt and check no role. `NodeResult.outcome` is a string enum whose values are the outcome names, to be reported as they are, except `SKIPPED_UP`, which the agent reports as `FAILED` with the result's detail. A busy node is `FAILED` with `another operation is in progress`. A failed start carries `exit_code` and a detail that includes the hint; the node's log is `nodeops.log_path(org, n)`. **The single-node functions raise on unexpected errors** (store, API or authentication errors, a missing file in the shared install), so the agent wraps each call and reports `FAILED` with the message; `stop_nodes_to_down` never raises for a node. **Order:** `stop_nodes_to_down` stops in descending node order with its pauses and polls the control plane as each node's own service account; for `START` and `RESET` the agent loops over its nodes itself, in ascending order, and a `NEW` node is reset without a control plane check. Opening the store can raise `DuplicatePortsError` or `SchemaVersionError` (both `StoreError`, with a message saying what to do): the agent treats either as a configuration error (exit status 78, §2.4). **Do not change the node spawn** to pass descriptors on (`pass_fds`, `close_fds=False`): the node must never hold the lifecycle lock. The store (`SCHEMA_VERSION` 10) has the `agent` table but no store functions for it yet (`add_agent`, `set_agent_pid`, `set_agent_stopped` and a lookup are the agent step's, using `_store(immediate=True)` for read-then-writes), and `issued_command` has `record_issued_command` and `list_issued_commands`. `nodeops.repair(node)` takes a try-lock and writes only a compare-and-set, so `vl node list` beside a running agent never clobbers a start in progress.

## 9. Tests

- **Command handling, with fakes** for the node operations, the producer and the clock: duplicate dropped; not mine ignored; wrong org refused; expired gives `EXPIRED` for each of my targets; expiry checked only at the start; order of targets kept; an unknown node gives `FAILED`; a poison message is skipped and committed.
- **Outcome mapping**: each row of §4.3, including the exit-code hint for a failed `START`, and `ALREADY_RUNNING` and `ALREADY_STOPPED`.
- **`RESET`**: refused while a process is alive; never stops a node; removes `data` only, or `data` and `snapshots` with the option.
- **Concurrency**: two threads or processes starting the same node produce one `STARTED` and one `ALREADY_RUNNING` or a busy `FAILED`, never two live processes; repair does not clobber a start in progress; two `create` calls never share a port.
- **Heartbeat**: each answer in §5, including re-registration on `404`.
- **Startup**: each outcome of §2.3, and the exit codes.
- **Results file**: only the header (once, at the top of a new or rotated file, not at an agent restart) and pipe-delimited rows ever reach `data/results.log`, and no general log line does; the file and its directory are created if missing; a detail containing a comma needs no quoting, one containing a pipe or a quote is quoted and round-trips through Python's `csv` reader with `delimiter='|'`, and a newline becomes a space so that one result is one line; each row is flushed at once; rotation at the size limit keeps the configured number of files; UTF-8 under `LANG=C`; own org only by default, all orgs with the setting; `org` falls back to the id; the `command` text of the result message is written as it is; `tail -F` follows across a rotation.
- **Node survival**: stopping the agent leaves the nodes running.
- **Integration**, against a real Kafka and a stub control plane: a command in, results out, in order.
