# veritaslock-cli (`vl`) — Node Agent Support: remote commands, `vl agent`, `vl command`

**Status:** Design settled after the owner's review. Not built; adjustments are expected once it is.
**Spec 3 of 3.** Spec 1 is `cp-agent-commands-spec.md` (the control plane: tables, topics, REST, roles). Spec 2 is `vl-agent-spec.md` (the agent program). This one covers everything that changes in `vl` itself, and how the three are built in order.
**Builds on:** `vl-node-spec.md` (the `node` table, `start`/`stop`/`reset`, the `/proc` checks), `vl-org-phase2-spec.md` §8 and `vl-network-spec.md` (the fleet verbs), `vl-service-account-spec.md`, `vl-authorization-spec.md`, `vl-env-spec.md`.

---

## 1. Scope

In scope:

1. **Shared node operations** that return structured results and report progress through a callback, so that the CLI and the agent run the same code (§4).
2. **Locking and store settings** so several processes can share `store.db` safely (§5).
3. **`vl agent`**: create, start, stop, list and results of the agent on this host (§6).
4. **Remote commands**: `vl network|org|node start|stop|reset` sending one command through the control plane when some node in scope is on another host, and waiting for the results by default (§7).
5. **`vl command`**: show, watch and list commands (§8).
6. **Failures are no longer swallowed** by the network and org loops (§9).
7. `NODE_AGENT` as a service-account role, new dependencies and entry points (§10).

Out of scope: the control plane (spec 1), the agent's runtime behavior (spec 2), the `verilock --print-host` flag (a small node task), packaging in containers, restarting dead nodes, and SSH-based remote start (`vl-node-spec.md` §7).

---

## 2. Local or remote: decided by the nodes in scope

There is no setting. For `vl network|org start|stop|reset`, `vl` works out which nodes are in scope and where they are:

1. **Resolve the scope.** Ask the control plane for the nodes in scope (the existing node listing, `GET /v1/nodes`, with the operator's identity, scoped as in `cp-node-identity-spec.md` §2.5), and add this host's `NEW` nodes, which the control plane does not know yet and which can only be local.
2. **A node is local** if a row for it exists in this host's `store.db`. Once `verilock --print-host` exists, its registered `host` must also equal this host's address: a stale, copied row counts as remote, so `vl` never starts a second copy of a node that runs elsewhere.
3. **If every node in scope is local, the verb runs locally**, as it always has: the local loops, synchronous, ordered (GLOBO first on start and last on stop), with no agent, no Kafka and no wait. **If any node in scope is remote, the whole verb goes through the control plane** as one command (§7), the local nodes included (the local agent handles them), and it is unordered across orgs (`vl-agent-spec.md` §8 item 1).
4. **`--local`** forces the local loop for this host's nodes only; **`--remote`** forces the control plane path even when everything is local, which is how the agents are exercised on a single machine. Both exist on the org and network verbs; `--remote` also on the node verbs.

When `vl` goes remote it says why once: `Not every node of org globo is on this machine (remote: globo/4, globo/5): sending the command through the control plane.`

| Verb | Every node in scope is local | Some node is remote |
|---|---|---|
| `vl network start\|stop` | The local loops, ordered | One command, `POST /v1/network/commands` |
| `vl network reset` | The control plane reset, then local org resets | The control plane reset, then one network-scope `RESET` (§7.3) |
| `vl org start\|stop\|reset` | The local loops | One command, `POST /v1/orgs/{orgId}/commands` |
| `vl node start\|stop\|reset` | A node with a row in the local store is always local, unchanged | A node not in the local store: a node-scope command, `POST /v1/orgs/{orgId}/nodes/{n}/commands` |
| `vl node create`, `list`, `show` | Local | Local |

**If the control plane cannot be reached, the verb aborts** with an error and changes nothing: `Cannot reach the control plane to find the nodes in scope; nothing was done. Use --local to act on this host's nodes only.` It exits **4** (§7.6). Acting on a partial scope without saying so is the dangerous option, so the way to ask for it is explicit (`--local`). A `401` or `403` from the listing is reported as the control plane's own error, and also aborts. (`vl node list` keeps its own rule, a warning and the local rows, because it only displays.)

**A consequence worth knowing.** The same verb can behave differently as an environment grows: on one machine `vl network stop` is the ordered, synchronous local loop; once a node exists on a second host, it becomes one unordered command through the control plane. That follows from where the nodes are, not from agents coming and going, and `vl` says so when it happens. Resolving the scope costs one extra read of the control plane per verb.


**Remote nodes are not in the local store.** A `node` row is created only by `vl node create`, on the machine that runs the node, because it holds a `svc_acct_id` (`NOT NULL`) that points at the node's own service account, whose keypair lives on that host. The table's `host` column is nullable and unused (`vl-node-spec.md` §4b, reserved for the SSH idea that agents replace). So a remote node has no local row, `vl node list` shows only this machine's nodes (it queries the control plane only to label them), and the control plane's whole-environment view is `vl network status`, which needs platform-wide standing.

### 2.1 Seeing the scope: `vl node list --remote` and `--all`

So that an `ORG_ADMIN` can see their remote nodes, and anyone can see what a verb is about to act on, `vl node list` gains the options `vl svc-acct list` already has:

- **`vl node list`**: unchanged: this machine's nodes, with the displayed states of `vl-node-spec.md` §4c.
- **`vl node list --remote`**: the control plane's listing for the caller's scope, on any host, and nothing local: org, node, `host`, port, state, and whether this host has a row for it. It is the same call as step 1 above.
- **`vl node list --all`**: the local rows plus the remote nodes that have no local row, one table, with the `host` column filled in for both.

`vl node list --all --org globo` therefore shows exactly the scope `vl org stop globo` would resolve, and which nodes are local and which remote, before anything is run. Like every `vl` option, these are long options.
---

## 3. Store changes

One migration; bump `PRAGMA user_version` by one (the build reads the current value; `vl-env-spec.md` §8).

| Change | Notes |
|---|---|
| `node`: a unique constraint on `port` | Port allocation (`MAX(port)+1` across the store) stays, but runs inside `BEGIN IMMEDIATE` (§5) and the constraint backs it up. |
| New table **`agent`** | One row per agent created on this host, like `node`: `id`, `environment_name`, `org_name`, `svc_acct_id` (the `NODE_AGENT` account, `REFERENCES svc_acct(identity_id) ON DELETE RESTRICT`), `pid`, `state` (`NEW`, `RUNNING`, `STOPPED`), `created_at`. Unique on `(environment_name, org_name)`: one agent per org and environment on a host, matching the control plane's rule. |
| New table **`command_history`** | The commands issued from this machine, so `--last` works: `id`, `environment_name`, `correlation_id`, `command_type`, `scope`, `org_name`, `org_node`, `issued_at`, `expires_at`. Trimmed to the newest 50 per environment on insert. |

`svc_acct.role` accepts `NODE_AGENT` (§10).

---

## 4. Shared node operations

Today `start`, `stop` and `reset` are Typer commands that print and return nothing (`reset` returns a bool since `org-reset-requires-down`). The agent needs the result, and its progress text belongs in its log, not on a console. So each verb is split in two:

- **A function** that does the work and returns a `NodeResult`, and
- **a thin command** that calls it and prints.

```
NodeResult(outcome, detail, exit_code=None, pid=None)
outcome ∈ STARTED | ALREADY_RUNNING | STOPPED | ALREADY_STOPPED | RESET | SKIPPED_UP | FAILED
```

The outcomes are those of `cp-agent-commands-spec.md` §3.4, plus `SKIPPED_UP` (reset found the node up), which the agent reports as `FAILED` with that detail. The functions take:

- **`emit(text)`**, a callback for the progress text they print today (`Started …`, `still reported UP …`). The CLI passes `print`; the agent passes its logger.
- **`confirm=False`**, so no function ever prompts. Prompting stays in the thin commands.

No function checks the operator's role (`require_operator` stays in the thin commands: the CLI checks locally, and for the agent the control plane already did).

| Function | Wraps | Result mapping |
|---|---|---|
| `start_node` | `vl node start` §5.2 | `STARTED` (pid), `ALREADY_RUNNING`, `FAILED` (exit code, the hint text `vl` prints today) |
| `stop_node` | `vl node stop` §5.3 | `STOPPED`, `ALREADY_STOPPED`, `FAILED` |
| `stop_nodes_to_down` | `stop_nodes`, the org routine of `org-reset-requires-down` | per node: `stop_node`, then the poll of the control plane as the node's own account until `DOWN`; `FAILED` ("still reported UP") when the poll times out. The pauses between nodes stay. This is what the agent calls for `STOP`. |
| `reset_node` | `vl node reset` §5.4 | `RESET`, `SKIPPED_UP`, `FAILED` |

`vl org start|stop|reset` and the agent then differ only in what surrounds the same calls.

---

## 5. Locking and store settings

Requirements from `vl-agent-spec.md` §6, built into the shared code:

1. **A per-node lock**: an advisory `flock` on `<node_root>/.lifecycle.lock` (outside `data/`, which `reset` deletes), held for the whole of `start_node`, `stop_node`, `reset_node` and by the repair that `vl node list` and `show` make when they find a dead pid. A person or an agent that finds it taken waits up to 10 seconds, then gets `FAILED` with "another operation is in progress". The repair takes the lock with a try, and skips itself if the node is busy.
2. **Store settings**, applied on every connection: `PRAGMA journal_mode=WAL` (persistent), `PRAGMA busy_timeout=10000`, and `BEGIN IMMEDIATE` for every transaction that reads and then writes (state changes, repair, port allocation).
3. **A consequence of WAL:** `store.db` gets `-wal` and `-shm` files beside it. Copying only `store.db` can lose recent writes, so any backup must copy all three or use `sqlite3 .backup`. The store must be on a local disk.

---

## 6. `vl agent`

All local (no REST call except the control plane columns of `list`), like `vl node`.

**Nothing is copied, at `create` or at `start`.** Unlike `vl node start`, which copies `$HOME/.veritaslock/bin/*`, the libraries and the configuration into each node's directory (`vl-node-spec.md` §5.2 step 2) so that every node runs its own binary, the agent is Python code in the installed `vl` package and runs from it (`python -m vl.agent`, with the same interpreter): a copy would only be a second version of the same code, stale after the next `vl` upgrade, and an upgrade takes effect when the agent is restarted. The one binary it needs is `verilock --print-host` (`vl-agent-spec.md` §8 item 2), which it reads from the shared install, `$HOME/.veritaslock/bin/verilock` by default, or from `--verilock <path>`. The agent's directory is therefore small: `agent.properties` (optional) and `data/`, with no `bin/`, `lib/` or `etc/`.

- **`vl agent create <org> [--name <n>] [--as <label>] [--env <env>]`**: creates the `NODE_AGENT` service account (the `vl service-account add` logic, display name `<org>-agent`, role `NODE_AGENT`, that org) and the `agent` row (`NEW`), and makes `$HOME/orgs/<org>/agent/` with its `data/`. Allowed for an `ORG_ADMIN` of the org, checked locally. The identity provider enforces the rule that applies to every service-account role (a user with an `ORG_ADMIN` row in the account's org, or a `SYSTEM` service account); a `PLATFORM_ADMIN` with no `ORG_ADMIN` row in that org is refused, so `vl` does not offer it. `vl service-account add --role NODE_AGENT` is a separate change in this repo (§10).
- **`vl agent start <org> [--env <env>]`**: operator role check, then it spawns `[sys.executable, "-m", "vl.agent", "--as", <label>, "--env", <env>]` detached (`start_new_session=True`), **stdout and stderr to `$HOME/tmp/<org>-agent.out`** (the general log, as for nodes; the results go to the agent's own `data/results.log`, `vl-agent-spec.md` §2.2), records `pid` and `RUNNING`, waits one second and checks it is alive. An early exit is a `CliError` with the log tail and a hint for the agent's exit codes (`vl-agent-spec.md` §2.4: 69 control plane or Kafka unreachable, 77 authentication refused, 78 bad configuration or identity).
- **`vl agent stop <org>`**: `SIGTERM`, wait up to 30 seconds (the agent finishes the node operation in hand), then `SIGKILL`. Nodes are not touched. Clears `pid`, sets `STOPPED`.
- **`vl agent list [<org>] [--remote] [--as <label>] [--env <env>]`**: named and shaped like `vl node list` (local rows with the control plane's view joined in) and `vl svc-acct list` (which has the same `--remote`). By default it lists the agents created on **this host**, from the local rows, in a bordered table: org, local state (`NEW`, `RUNNING`, `STOPPED`, with the pid checked against `/proc` like `vl node list`), the control plane's state for the agent (`ALIVE`, `STALE`, `NEVER_SEEN`), `host`, `nodeCount` and when it was last seen (from `GET /v1/agents`). A local `RUNNING` agent that the control plane calls `STALE`, or a `nodeCount` of zero, is highlighted: those are the two symptoms of a `host` mismatch (`cp-agent-commands-spec.md` §3.3). **`--remote`** lists every agent the control plane knows for the orgs you may see, on any host, with no local columns: it answers "which hosts have an agent?". The control plane columns need an operator identity (`--as` or the default identity); without one, or if the control plane cannot be reached, the local columns are shown, the others are `-`, and one line says why. The optional `<org>` narrows the list. There is no `vl agent show` for now: the list carries what it would.
- **`vl agent results [<org>] [--limit <n>] [--all] [--follow] [--details] [--env <env>]`**: reads the results the agent writes itself, `$HOME/orgs/<org>/agent/data/results.log` and its rotated files (`results.log.1`, …, `.4`), which are pipe-delimited text (`vl-agent-spec.md` §4.4), so it works however the agent was started and after it has stopped. Like every `vl` option, these are long options; there are no short forms.
  - **On a terminal**, a bordered table like the other list commands, with these columns in this order: **`COMMAND`** (16; `vl network start` and `vl network reset` are 16 characters), **`NODE`** (`<org>/<n>`, 14), **`OUTCOME`** (15; `ALREADY_RUNNING` and `ALREADY_STOPPED` are 15), **`TIME`** (24), **`ID`** (36), **`DETAIL`** (the rest of the width, wrapped). `NODE` is added to the five originally asked for: an org or network command produces one row per node, and without the node the rows cannot be told apart. `agent`, `host`, `exit` and `org` stay in the file (use `grep`, or open it).
  - **`DETAIL` is shown when it fits.** The five fixed columns need about 125 characters with the table's borders, so detail needs a terminal of about 150. On a narrower one the column is left out and `vl` says so in one line (`Terminal too narrow for DETAIL (need 150, have 120): widen it, or use --details`). **`--details`** forces it in anyway, wrapping cells to the width available.
  - **Not on a terminal**, that is when the output goes through a pipe (`vl agent results | grep FAILED`) or is redirected to a file, the output is **the file's own text**: the header once, then the rows, pipe-delimited, in the same order, all columns. `vl agent results --all | grep FAILED` and a spreadsheet then work with no further options.
  - **Default (no option): the header, then the last 20 results, newest first, and exit.** A read command finishes by default, as `vl command show` and `vl node list` do.
  - **`--limit <n>`**: that many results (at least 1), newest first.
  - **`--all`**: every retained file, newest first: at most 50 MiB (5 files of 10 MiB), meant for `less` and `grep`.
  - **`--follow`**: the last `--limit` results (default 20) in **chronological** order, then it follows the current `results.log` as it grows, across a rotation (`tail -F`), until Ctrl-C (exit 0). On a terminal it prints one plain line per result, columns aligned to the widths above, detail last and not wrapped, with no borders (a stream cannot close a table); not on a terminal it streams the file's lines. `--limit 50 --follow` is allowed.
  - **`--all` cannot be combined with `--limit` or `--follow`** (a usage error, exit 2).
  - **One result is one line**, so counting and ordering are by line. The header (the file starts with one, and each rotated file does) is never counted as a result. If the current file has fewer results than the limit, the rest come from the rotated files, newest first, so a rotation does not leave a short answer.

**Omitting the org.** The agent rows are per environment, so the usual environment resolution applies first (`--env`, else the default environment), and the counts below are within it.

| Verb | Org omitted |
|---|---|
| `vl agent list` | Shows every agent on this host, one row each (`<org>` only narrows it). |
| `vl agent results` | If exactly one agent exists on this host, it is used. If there is more than one, it stops with an error naming them (`More than one agent on this host: acme, globo. Name one: vl agent results globo`); it never merges the orgs' files. If there is none, it says so and points at `vl agent create`. |
| `vl agent create`, `start`, `stop` | The org is required. These change state, so `vl` makes you name the org, as `vl node start <org> <n>` does. |

An `--all-orgs` option for `results`, interleaving rows by time, is a possible later addition.

---

## 7. Remote commands

### 7.1 Flow (some node in scope is remote, or `--remote`)

1. The operator role check, locally, as today (`vl-authorization-spec.md`). A network command needs a `PLATFORM_ADMIN` **user** identity; a service-account identity is refused here with a clear message, as the control plane would.
2. The REST call to the control plane with the operator's token (`--as` or the default identity). The org is resolved to its id from the local organization cache. The request carries **`command`**, the command as typed, without its arguments (`vl network stop`), which the results later show; the `RESET` that `vl network reset` issues carries `vl network reset` (`cp-agent-commands-spec.md` §5.1).
3. On `202`, record the command in `command_history`, print what was published, and **wait for the results** (§7.5) unless `--no-wait` was given:

```
$ vl network stop --as admin
Network stop published to 3 hosts (5 nodes). Command 7f3a1c2e-5b6d-7a10-8e21-3c9d4f5a6b7c
  Skipped (no agent): globo/7, globo/8
  Never started on this machine, so not reachable by remote commands: globo/9
Waiting for 5 results (Ctrl-C stops waiting; the command keeps running)…
  globo/3   STOPPED          2026-10-27T09:14:06.412Z  pid 48213
  globo/4   FAILED           2026-10-27T09:14:07.093Z  still reported UP by the control plane
  …
4 of 5 succeeded. See: vl command show 7f3a1c2e-5b6d-7a10-8e21-3c9d4f5a6b7c
```

With **`--no-wait`** the command prints `Look at the result: vl command show --last   Wait for it: vl command watch --last` and exits **0** once published: it has not tried to find out whether the agents succeeded. Without it, the exit status follows the results (§7.5, §7.6). `409 NO_TARGETS` exits 1 with the control plane's message, and a `RESET` refused because a node is not `DOWN` exits 1 naming each node (`cp-agent-commands-spec.md` §5.1).

### 7.2 Hints, computed locally

- **Local nodes that were never started** (`state = NEW`) within the command's scope: the control plane does not know them (`cp-agent-commands-spec.md` open item 6), so they appear in neither `targets` nor `skipped`. `vl` lists them as above.
- **A local agent that is not running** for an org in the command's scope (the `agent` row is not `RUNNING`): `Warning: the agent for org globo on this host is not running; its nodes will not answer. Start it with vl agent start globo.`

### 7.3 `vl network reset` when some node is remote

The control plane reset first, as today (it requires every node `DOWN`, and checks it). Then **one network-scope `RESET` command** (`POST /v1/network/commands` with `"type": "RESET"`), with one id; the default wait follows it (§7.5), and the exit status follows §7.6. A node's data that was not reset (an agent that did not answer) is visible in `vl command show`, and the fix is to run `vl org reset <org>` again. A `409` on the `RESET` itself (a node came up between the two calls) names the nodes. Order matters little here: nothing is stopped.

### 7.4 Which identity

Remote verbs use the operator identity, as `vl network reset` already does for the control plane's reset endpoint. They need no node service account: the control plane decides, and the agent acts.


### 7.5 Waiting for the results

By default the remote verbs wait; **`--no-wait`** returns once the command is published. When the verb runs locally (every node in scope is local, or `--local`), the flag has no effect, because the local loops are already synchronous.

**Where the results come from.** The `202` response lists the targets, so `vl` knows who has to answer. For each org in the command's scope:

- If this host has a **running agent** for the org (the local `agent` row is `RUNNING` and its pid is that agent), `vl` reads that agent's `data/results.log`. The agent consumes every result for its org, from every host, and writes it there, so the file is a complete source for the org. `vl` records each file's size **before** it sends the request and reads from that point, so a result written at once is never missed, and it follows the file by name across a rotation. It keeps only the rows whose `id` is the command's.
- Otherwise (no local agent for that org, or it is not running) `vl` asks the control plane for that org's targets, `GET /v1/commands/{id}` every 2 seconds. Only those targets are polled.

The choice is made up front from the local `agent` rows, and said once when the control plane is used (`Waiting on the control plane for: acme (no running agent for it on this host)`). An admin machine with no agent at all therefore polls for everything, exactly as `vl command watch` would.

**What is printed.** One line per result as it arrives (node, outcome, time, detail), in the stream format of `vl agent results --follow`.

**When it ends.** When every target has answered, or at `waitUntil` (the response's `expiresAt` plus the control plane's grace period: at most about a minute after the command was issued), whichever comes first. Then **one** `GET /v1/commands/{id}` gives the authoritative answer: the status, the counts, and every target with its state, including any `NO_ANSWER` target and its agent's state, and any `SKIPPED` node. The **exit status comes from that answer and never from the lines that were printed** (§7.6). If the answer is still `PENDING` (the control plane's clock may be a moment behind), `vl` asks again every second for up to 5 seconds; anything still `PENDING` after that is reported as not answered. A bordered table of the targets that did not succeed is printed, then a closing line: `4 of 5 succeeded, 1 failed. Run the same command again to retry the node that failed.` or `4 of 5 succeeded; 1 did not answer.` (the commands are idempotent, so running them again only does the rest), with `See: vl command show <id>`. If the final answer disagrees with the lines already printed, the final answer wins. If the control plane **cannot be reached** for the final `GET`, `vl` prints the lines already seen and the id, says that the outcome could not be confirmed (`vl command show <id>` will tell), and exits **4**. **Ctrl-C** stops waiting, not the command: it prints the id and the `show` hint and exits 130.

### 7.6 Exit codes of the remote verbs and `vl command watch`

A script can tell the cases apart by the exit status alone:

| Code | Meaning | The command's status, or the situation |
|---|---|---|
| **0** | Every target succeeded (and with `--no-wait`: the command was published) | `SUCCEEDED` |
| **1** | A **hard failure**: at least one target failed, or the control plane refused the request (`401`, `403`, `404`, `409` including `NO_TARGETS` and a refused `RESET`) | `FAILED`, or the refusal |
| **2** | A usage error: a bad option or argument, as everywhere in `vl` | (not about the command) |
| **3** | **Incomplete**: no target failed, but some did not answer or were skipped because they have no agent, or an explicit `vl command watch --timeout` ran out first | `INCOMPLETE`, or a still `PENDING` answer |
| **4** | The **control plane could not be reached**, when resolving the scope, issuing the command, or asking for the final answer: the outcome is unknown | (no answer) |
| 130 | Interrupted with Ctrl-C; the command keeps running | |

When several apply, the order is 4, then 1, then 3, then 0: not knowing outranks a failure, and a failure outranks a node that did not answer. A script that only cares whether everything worked tests for 0; one that wants to retry tests for 3; one that wants to alert tests for 1 and 4. The status names (`FAILED`, `INCOMPLETE`) are those of `cp-agent-commands-spec.md` §6.2, so the two cannot disagree.
---

## 8. `vl command`

- **`vl command show [<id> | --last] [--as <label>] [--env <env>]`**: `GET /v1/commands/{id}`. A header line (type, scope, issuer, issued, expires, overall status `PENDING`, `SUCCEEDED`, `FAILED` or `INCOMPLETE`, and the counts: `4 of 5 succeeded, 1 failed`) and a bordered table, one row per target, **including the nodes that were skipped for having no agent**: agent, host, node, state (`DONE`, `FAILED`, `SKIPPED`, `PENDING`, `NO_ANSWER`), outcome, exit code, detail. For `NO_ANSWER` the agent's own state is shown (`ALIVE`, `STALE`, `NEVER_SEEN`, with when it was last seen). Exit 0 for a command that exists, 1 for an unknown id, 4 if the control plane cannot be reached.
- **`vl command watch [<id> | --last] [--timeout <seconds>] [--interval <seconds>]`**: waits for a command that has already been issued, with exactly the logic of §7.5 (the local agent's results file for the orgs this host has a running agent for, the control plane for the rest), so the remote verbs and `watch` are the same code. Use it after `--no-wait`, or for a command issued from another machine. `--interval` is the polling interval for control plane targets (default 2 seconds); `--timeout` caps the wait (default: until `waitUntil`). The exit status is that of §7.6; an explicit `--timeout` that runs out before every target has answered is **3**.
- **`vl command list [--org <org>] [--limit <n>]`**: `GET /v1/commands`, newest first: id, type, scope, issuer, issued, status.

`--last` is the newest row of `command_history` for the environment. Because the verbs wait by default and exit non-zero on failure, a script chains them directly: `vl network stop --as admin && vl network reset --as admin --yes`. With `--no-wait`, `vl command watch --last` is the explicit wait.

---

## 9. Failures are no longer swallowed

`vl network start|stop|reset` and `vl org start|stop` wrap each org or node in a handler that turns any exception, including a refusal, into `Warning: …`, so the command exits 0 even when something failed (`vl-network-spec.md` open item 5). Changed, for the local loops (every node in scope is local, or `--local`):

- the per-node and per-org warnings stay, as they are the detail;
- every failure is also collected, and at the end the command prints **`N of M nodes failed:`** with one line each;
- the exit status is **1** if anything failed.

A node that was already running or stopped is not a failure.

---

## 10. Dependencies, role, entry points

- **`vl service-account add --role`** accepts `NODE_AGENT` (the choices list, and the `svc_acct.role` column if it has a constraint, which the build checks). `vl svc-acct list --role` filters on it. The identity provider side is a separate task (`node-agent-role`).
- **Dependencies:** an optional group `vl[agent]`: `confluent-kafka` with its Avro and Schema Registry support (`fastavro` and the HTTP client). Ordinary `vl` hosts do not install it. `vl agent start` fails with a clear message ("install vl[agent]") if it is missing.
- **Entry points:** `vl` as today, and `vl-agent` (foreground, the agent program of spec 2). `vl agent start` runs `python -m vl.agent` with the same interpreter, so it needs no `PATH` lookup.

---

## 11. Build order across the three specs

1. **Independent and small, first:** the `NODE_AGENT` role in the identity provider; `verilock --print-host` in the node.
2. **`vl`, no dependency on anything new:** §4 (shared functions and results), §5 (locking and store settings), §9 (failures not swallowed), and the first step of the build: confirm that `vl node start` really spawns with `start_new_session=True` (`vl-agent-spec.md` §8 item 6).
3. **The control plane** (spec 1): tables, topics, schemas, endpoints, consumer, cleanup.
4. **The agent program** (spec 2) with `vl agent` (§6) and `vl service-account --role NODE_AGENT`.
5. **Remote verbs and `vl command`** (§7, §8), with the scope resolution of §2.
6. **Acceptance on two real hosts**, the desktop and the laptop: register an agent on each, start a network that spans both, stop it, reset it, and look at each result with `vl command show`.

Steps 1 and 2 can run while spec 1 is still being reviewed; they change nothing a user sees.

---

## 12. Tests

- **Shared functions:** each outcome of §4, including the exit-code hint for a failed `start`; `emit` receives every progress line and `print` is never called directly; no function prompts.
- **Locking:** two processes starting the same node give one `STARTED` and one `ALREADY_RUNNING` or a busy `FAILED`, never two live processes; repair does not clobber a start in progress; two concurrent `create` calls never share a port; the wait is 10 seconds and then fails.
- **Store:** the migration from the previous version; WAL and the busy timeout are set on every connection; the unique port constraint.
- **Local or remote (§2):** every row of the table; a scope that is entirely local runs the local loops with no control plane command; one remote node sends the whole verb through the control plane, local nodes included, and says why once; `--local` and `--remote`; a stale copied row counts as remote once `--print-host` exists; a `NEW` node never makes a scope remote; the control plane unreachable aborts with the `--local` hint and changes nothing, and a `401` or `403` from the listing aborts with the control plane's error; a node in the local store is local without any lookup.
- **`vl node list --remote` and `--all`:** `--remote` shows the control plane's nodes for the caller's scope and nothing local; `--all` shows local rows plus the remote ones with no local row, with `host` in both; the plain command is unchanged; the scope shown by `--all --org <org>` is the one the fleet verbs resolve; without an identity, or with a `401` or `403`, nothing is printed but the error; the control plane unreachable gives `--all` the warning and the local rows.
- **Remote verbs:** the role check comes first; the request and the printed output for network, org and node scope; skipped nodes and local `NEW` nodes listed; the not-running-agent warning; `NO_TARGETS`; a refused `RESET`. **The wait:** results printed as they arrive from the local agent's file; the file's size recorded before the request, so a result written at once is not missed; a rotation during the wait; an org with no running local agent polled from the control plane, and the choice said once; the final `GET` decides the exit status (0 or 1) and prints the targets that did not succeed; give-up at `waitUntil` with `NO_ANSWER`; Ctrl-C exits 130 and leaves the command running; `--no-wait` prints the id and exits 0; local mode ignores the flag; `vl network reset` issues its one network-scope `RESET` after the control plane reset and waits for it.
- **`vl network reset`:** the control plane reset, then one network-scope `RESET`; a refusal at the control plane stops before any command; a `409` on the `RESET` names the nodes.
- **`vl command`:** `show` for each state including `NO_ANSWER` with the agent's state, `SKIPPED` rows, the counts and each overall status; `show` exits 1 for an unknown id and 4 when the control plane is unreachable; `watch` exits as §7.6 says, including 3 for an explicit `--timeout`; `--last`; `list`; history trimmed to 50.
- **Exit codes (§7.6):** every row of the table, for the remote verbs and for `watch`: all targets succeeded gives 0; four successes and one failure gives 1; no failure but one unanswered gives 3; no failure but one skipped gives 3; a failure together with an unanswered node gives 1; the control plane unreachable when resolving the scope, when issuing, and at the final check gives 4 even if lines were printed before; a usage error still gives 2; an answer still `PENDING` after `waitUntil` is asked again every second for 5 seconds and then gives 3; the exit status never depends on the lines read from the file.
- **`vl agent`:** create (with its `data/` directory), start (with each exit-code hint), stop (including the `SIGKILL` escalation), list with a mismatch highlighted, `--remote`, and with no identity or no control plane (local columns only, one line saying why); results: the default is the last 20 newest first, and it exits; `--limit`; `--all` newest first across every file; `--follow` prints chronologically and follows across a rotation, with `--limit` allowed alongside; `--all` with `--limit` or `--follow` is a usage error (exit 2); the column order and widths on a terminal; `NODE` shown as `<org>/<n>`; `DETAIL` omitted with the one-line hint when the terminal is too narrow, and forced by `--details`; pipe-delimited text, header once, when the output is piped or redirected; the header is never counted as a result; a short current file is filled from the rotated ones; it reads the files after the agent has stopped; with the org omitted: one agent is used, several give the error naming them, none points at `create`; `list` lists all.
- **Failures:** the summary and exit status for each loop, with a node already running not counted.

---

## 13. Open items (decisions for the owner)

1. **Local or remote. Decided: by the nodes in scope, with no setting.** If every node in scope is local the verb runs locally (ordered, synchronous, no agent); if any is remote the whole verb goes through the control plane (§2). `--local` and `--remote` override. This replaces an `environment.agents` setting, and a rule based on which agents happen to be registered: the behavior now follows where the nodes are, which changes rarely, and an environment that lives on one machine never needs an agent.
2. **`vl agent delete`. Decided: later.** The control plane has `DELETE /v1/agents/{id}`, and a verb to call it can be added when removing an agent stops being rare.
3. **`vl command watch` defaults. Decided.** A 2-second interval for control plane targets, no timeout beyond `waitUntil`, and the exit codes of §7.6 (an explicit `--timeout` that runs out is 3).
4. **`vl network reset` when some node is remote. Decided:** reset the control plane first, then the node data (the same order as today). If an agent does not answer, the control plane's data is gone and that node still has its old chain until `vl org reset` is run again. The alternative, node data first, leaves the opposite inconsistency if the control plane then refuses.
5. **Command history size. Decided.** The newest 50 per environment.
6. **The shared functions move into a library module. Decided** (for example `vl.lib.nodeops`), so the agent does not import from `commands/*.py`.
7. **Migration and the version number. Nothing to decide.** The build reads the current `PRAGMA user_version` and the existing migration mechanism, and bumps the version by one, as earlier phases did.
8. **The agent's label. Decided.** `vl agent create` names the service account `<org>-agent` by default.
9. **Waiting by default. Decided.** When a verb goes through the control plane it waits for the results and exits with their status, as the local loops always have, and `--no-wait` is the opt-out. The results come from the local agent's file where this host has a running agent for the org, and from the control plane only for the rest. This replaces the earlier design, in which the verbs returned at once and a separate `vl command watch` polled the control plane.
10. **`vl node list --remote` and `--all`.** Remote nodes have no local rows, so without these an org admin cannot see their remote nodes at all, and nobody can preview what a fleet verb will act on. **Decided, as written in §2.1.** The alternative, leaving `vl node list` local and relying on `vl network status`, which needs platform-wide standing, was not taken.
