# veritaslock-cli (`vl`) — Phase 7: `vl node`

**Status:** Draft for implementation
**Part of:** the larger `vl-cli-phase1-crud-spec.md` effort, broken out as its own standalone document.
**Depends on:** `vl-env-spec.md` (Phase 1, for `environment` — this document extends its schema), `vl-service-account-spec.md` (Phase 4, for `identity`/`svc_acct` — `vl node create` creates a `NODE`-role service account via the same code path `vl svc-acct add` uses, not a parallel one), `vl-org-spec.md` (Phase 2, for the `organization` cache table `node.org_name` FKs against), `vl-org-phase2-spec.md` §8 (the org-scoped `start`/`stop`/`reset` orchestration built on top of this document's `vl node` verbs — not yet merged into `vl-org-spec.md` itself, see that document's own scope note).
**Builds on:** `start_test_harness.sh` — this phase re-implements that script's per-node start/stop/reset mechanics (spawn, kill, CP-status check) as the `vl node` verbs, using `store.db` as the source of truth for node identity and configuration instead of files staged under each node's `VERITAS_INSTALL_ROOT`. Fleet-level orchestration (the script's start/shutdown loops and their ordering/stagger) is specced separately in `vl-network-spec.md` (Phase 8) and `vl-org-phase2-spec.md` §8, both built on top of this document's verbs rather than repeating their mechanics.
**Schema baseline:** current store schema version is `8` (post `vl team` migration). This document bumps to `9`.

---

## 1. Scope

This document covers:
- Two additions to the existing `environment` table (`token_url`, `schema_registry_url` — present in the running system but not yet modeled in the store).
- A new `env_kafka_property` table — the open-ended librdkafka consumer/producer properties (`enable.auto.commit`, `session.timeout.ms`, etc.), which don't vary per node and are unlikely to vary per environment either, but are genuinely open-ended, unlike the fixed URL set above.
- A new `node` table — one row per provisioned node, scoped to an org.
- The `vl node` command group: `create`, `start`, `stop`, `reset`. (No `list`/`show`/`update`/`delete` in this phase — see §6 Open Items.)

**Not covered here:** fleet-level orchestration. `vl org start`/`stop`/`reset` is specced in `vl-org-phase2-spec.md` §8, and `vl network start`/`stop`/`reset` in `vl-network-spec.md` (Phase 8) — both are thin orchestration layered on top of this document's `vl node` verbs, kept in their own documents so `vl org`'s and `vl network`'s command surfaces are each documented in one place.

**Explicitly out of scope:** `send_events.sh`'s migration (staying bash for now, ported to read `store.db` directly rather than hardcoded paths — separate, independent effort), and any Docker-based node runtime (the filesystem-rooted `$HOME/orgs/<org>/nodes/<name>` layout below is understood to be transitional).

**Planned fast-follow, not part of this phase:** remote node execution via SSH — running these same verbs against a node on another machine, given network connectivity and a pre-established SSH keypair. §4's `host` column is designed with this in mind; §7 lays out the direction.

---

## 2. Schema: `environment` (additive columns)

| Field | Type | Notes |
|---|---|---|
| token_url | text | full URL, e.g. `http://localhost:8080/auth/service-account/authenticate` — distinct from `idp_base_url`, which is the IdP's base; token issuance has its own path |
| schema_registry_url | text | full URL, e.g. `http://localhost:8081` |

Both `NOT NULL` for any environment used by `vl node start` — enforced at the point of use (a node can't start without them), not as a table-level constraint, so that `vl env add` for envs that never run nodes (unlikely in practice, but not this document's problem to forbid) isn't forced to supply them.

This brings `environment`'s full URL set to six named columns: `idp_base_url`, `cp_base_url`, `di_base_url`, `token_url`, `schema_registry_url`, `kafka_bootstrap`. Deliberately kept as named columns rather than folded into `env_kafka_property` or a generic key-value table — this is a small, fixed, well-known set that every environment has exactly one of each of; a key-value table earns its keep on §3's genuinely open-ended property list, not here.

## 3. Schema: `env_kafka_property`

| Field | Type | Notes |
|---|---|---|
| environment_name | text | `REFERENCES environment(name) ON UPDATE CASCADE ON DELETE CASCADE` |
| key | text | e.g. `session.timeout.ms`, `auto.offset.reset` |
| value | text | |

`PRIMARY KEY (environment_name, key)` — natural uniqueness (no duplicate property per env), and an easy `INSERT OR REPLACE` upsert pattern when seeding or updating.

`ON DELETE CASCADE` here (unlike `environment`'s own `RESTRICT` against most referencing tables, §3 of `vl-env-spec.md`) — a property row has no independent meaning without its parent environment, same reasoning as `team_member`'s cascade from `team`.

No per-node override in this phase. The current file-based setup supports one, but it's never been used in practice; if the need arises, a `node_kafka_property` table with the same shape, consulted first and falling back to `env_kafka_property`, is a purely additive follow-up.

No CLI commands to manage this table in this phase — populated by seed/migration only, per the schema bump noted in the header above. `vl node start` reads it to render the node's Kafka consumer config; nothing writes to it via `vl` yet.

## 4. Schema: `node`

| Field | Type | Notes |
|---|---|---|
| id | integer | `PRIMARY KEY AUTOINCREMENT` — surrogate key, not `node_id` (below), since `node_id` doesn't exist until the CP assigns one on first startup |
| environment_name | text | `REFERENCES environment(name) ON UPDATE CASCADE ON DELETE RESTRICT` |
| org_name | text | `REFERENCES organization(environment_name, name)` (composite, via `environment_name` above), same pattern as `team.org_name` |
| name | text | `node1`, `node2`, etc. — unique within an org, not globally |
| node_id | text | nullable, `UNIQUE` — assigned by the control plane on the node's first successful startup; `NULL` for a created-but-never-started node |
| host | text | `NOT NULL DEFAULT '127.0.0.1'` — the address `verilock` registers itself under with the control plane (`NodeDto.host`), used to query its status (`GET /nodes?host=&port=`). Set at creation via `--host` (§5.1); not necessarily the literal string `localhost` even for a locally-spawned node, since the CP's own `host` field holds an IP address, not a hostname alias |
| svc_acct_id | integer | `NOT NULL`, `REFERENCES svc_acct(identity_id) ON DELETE RESTRICT` — the `NODE`-role service account created alongside this row (§5.1); named for the table it points at, even though the column it targets is `svc_acct.identity_id` (that table's PK, `vl-service-account-spec.md` §2). `client_id` (`identity.principal_name`) and `client_secret` (`svc_acct.client_secret_plaintext`) are read via this FK, not duplicated onto `node` |
| port | integer | `NOT NULL` — auto-assigned starting at `7001`, globally unique within the environment (across all orgs, since nodes in the same env are expected to run concurrently); overridable via `--port` at creation |
| pid | integer | nullable — set on successful `start`, cleared on `stop`/detected-dead |
| status | text | `NOT NULL DEFAULT 'new'`, `CHECK (status IN ('new', 'running', 'stopped'))` |
| cp_state | text | nullable, `CHECK (cp_state IN ('UP', 'DOWN', 'STANDBY') OR cp_state IS NULL)` — best-effort mirror of the control plane's own `NodeDto.state` (§4a below), stored using the CP's own vocabulary rather than translated into `status`'s. `NULL` until the first successful CP query for this node (i.e. always `NULL` while `status = 'new'`) |
| cp_state_synced_at | text | nullable, ISO8601 — when `cp_state` was last successfully refreshed; `NULL` alongside `cp_state` |
| created_at | text | ISO8601 |

`UNIQUE (environment_name, org_name, name)`.

`ON DELETE RESTRICT` on `svc_acct_id` (rather than `CASCADE`, unlike most of this store's identity-owned children) — a `node` row is the more significant, longer-lived record here; deleting the underlying service account out from under an existing node should fail loudly rather than silently orphan the node's credentials. There's no `vl node delete` in this phase (§6, item 3) to make this concrete yet, but the constraint is worth setting correctly now rather than defaulting to `CASCADE` and revisiting later.

`status` is tracked as an explicit column rather than derived purely from `pid IS NOT NULL` — mildly redundant, but makes `WHERE status = 'running'` queries and CLI table output direct, and leaves room for a future state (`crashed`) without a schema change. `vl` is the sole writer of `pid`/`status` — no independent liveness scan reconciles them; a node started or killed outside `vl` (e.g. under a debugger, per §5.3) is simply invisible to this bookkeeping, which is the intended behavior, not a gap.

**`new` is distinct from `stopped`**, not a synonym for it: a node's control-plane counterpart doesn't exist until `vl node start` actually runs it for the first time — the CP has no row for a node that's only ever been `vl node create`d — so `stopped` (which implies "was running, isn't now") would be a misleading state for a node that has never been started at all. `new` is the only status set by `create` (§5.1); `start`'s first successful run (§5.2) is what transitions a node out of `new` for good, to `running`, and it never returns to `new` afterward — `stopped` (from `stop`, §5.3) is the only state a subsequent `start` finds it in. `vl node reset` (§5.4) treats `new` as a special case (see that section) since there's no CP-side node yet to check status against.

**`vl node start` (§5.2) does not itself use `host`** — spawning is always local to the machine `vl` runs on in this phase (`subprocess.Popen`, no remote-exec support). `host` exists purely so the CP status lookup in `stop`/`reset` (§5.3–§5.4) and the fleet-level orchestration built on them (`vl-network-spec.md` §3.2, `vl-org-phase2-spec.md` §8) query the CP with whatever address `verilock` actually registered under, rather than assuming it matches a hardcoded string. The `--host` override at creation (§5.1) is what makes this correct today, not just forward-looking for remote nodes: even a locally-run `verilock` may register an IP rather than the literal hostname `localhost`, and the two aren't guaranteed to match without this field.

### 4a. `cp_state` — best-effort refresh, not authoritative

The CP's own `NodeState` (`STANDBY` in addition to `UP`/`DOWN`) doesn't map cleanly onto `status`'s vocabulary, and keeping a local cache of it perfectly in sync isn't the goal — `cp_state` is a display/diagnostic aid, not something any `vl node` command's control flow branches on. (`status` remains the one column `vl`'s own logic trusts — the pre-check in `start`, §5.2; the not-running check in `stop`, §5.3 — because it's derived from `pid`, which `vl` actually controls.)

Every `vl node` verb that operates on a node with a known `node_id` (i.e. anything past `new`) refreshes `cp_state` as a side effect, using the same `GET /nodes?host=&port=` call and the same node-owns-its-own-credentials authentication already established for `stop`/`reset` (§5.3–§5.4):

- **`create`** — never refreshes; nothing CP-side exists yet for a brand-new node (`status = 'new'`).
- **`start`** — after the liveness check (§5.2 step 4) succeeds, best-effort refresh. A node that just started may not have registered with the CP yet by the time this fires — treat a failed/empty lookup the same as any other refresh failure (leave `cp_state`/`cp_state_synced_at` as they were, don't error the command over it).
- **`stop`** — best-effort refresh at the *start* of the command, before signaling — this is purely informational, separate from and unrelated to the CP-DOWN-confirmation polling that `vl network stop`/`vl org stop` perform after signaling (`vl-network-spec.md` §3.2), which remains a required, blocking call those orchestration layers already make regardless of this refresh.
- **`reset`** — the CP check §5.4 already performs (required, not best-effort, since `reset` refuses to proceed if the node is still up) doubles as this refresh — no separate call needed.

"Best-effort" means: on any failure (CP unreachable, node not found, timeout), leave `cp_state`/`cp_state_synced_at` unchanged and proceed with the command's normal logic — never block or fail a `start`/`stop`/`reset` because this side-channel refresh didn't succeed.

---

## 5. Commands: `vl node`

Every verb below also accepts `--help` explicitly (in addition to the existing "missing required argument prints help" behavior, `vl-command-restructure.md` §2) — a user can request it directly rather than only getting it by omitting something.

### 5.1 `vl node create --org <org> [--name <name>] [--port <port>] [--host <ip>] [--as <label>] [--env <env>]`

1. Resolve `<name>`: if omitted, generate `node<N>` where `N` is one greater than the highest `N` ever used for a `node<N>`-pattern name in this org (including deleted nodes — gaps from deletion are not reused; deliberately simpler than tracking historical names, and avoids confusion if a deleted node's old directory/logs are still present on disk). Explicitly-named nodes that don't match the `node<N>` pattern are ignored for this calculation.
2. Resolve `<port>`: if omitted, one greater than `MAX(port)` across every node in this environment (not just this org), starting from `7001` if none exist yet. Explicit `--port` overrides and is not validated for uniqueness in this phase — an operator-supplied collision is the operator's problem, consistent with `vl` not pre-checking things the server (or, here, the OS) will reject anyway.
3. Resolve `<host>`: `--host` if supplied, else `127.0.0.1` (§4). Not validated against anything — `vl node start` doesn't use it to decide where to spawn (§5.2), only `stop`/`reset` use it later to query the CP, so a wrong value here just means those two commands query the wrong address, not a broken start.
4. Create a `NODE`-role service account, calling the same underlying function `vl svc-acct add` uses (not a parallel implementation) — if this step fails, abort with no `node` row and no directory created.
5. Create `$HOME/orgs/<org>/nodes/<name>` on disk (hardcoded root for this phase — see §6 Open Items; not read from config or the store).
6. Insert the `node` row: `svc_acct_id` from step 4's resulting `svc_acct` row, `host` from step 3, `node_id = NULL`, `pid = NULL`, `status = 'new'`.

No caller-kind pre-check beyond what step 4's underlying service-account creation already enforces — same governing principle as `vl team add` (`vl-team-spec.md` §5.1) and `vl svc-acct add`'s own precedent.

### 5.2 `vl node start <org> <name> [--env <env>]`

No `--as` — nothing in this command calls the CP or IdP; it's a local spawn plus local `store.db` writes. (`create`, §5.1, is the only `vl node` verb that still takes `--as`, since it's the only one that calls the IdP as an operator.)

No separate `--profile` flag — `verilock`'s own environment-selection argument is redundant with `vl`'s `--env`, which already resolves this command's target environment (URLs, Kafka properties) throughout this document. `vl node start` passes the resolved environment's `name` straight through as `verilock --profile <env-name>`, keeping the two in lockstep rather than letting an operator specify them independently and have them drift apart. `--env` itself still defaults the normal way (§6 of `vl-env-spec.md` — the store's default environment if omitted), so there's no behavior change from before beyond dropping the redundant flag.

1. **Pre-check:** if the node's `status = 'running'` and `pid` is alive (`kill -0`), no-op with a message (`node1 is already running (pid <pid>)`) and exit — `verilock` also refuses to double-start, but checking first avoids the wasted setup/spawn/log-parse cycle below and gives a clearer message.
2. **Setup** (idempotent — re-run unconditionally on every start, matching current script behavior):
   - Copy `$HOME/.veritaslock/bin/*` → `<node_root>/bin`, `chmod +x`.
   - Copy `$HOME/.veritaslock/lib/liblog4cplus.so*` → `<node_root>/lib` (in-tree dependency, not a system package — `verilock` aborts on startup without it).
3. **Spawn:** `<node_root>/bin/verilock --profile <resolved env name>`, detached into its own session (Python equivalent of the script's `setsid`: `subprocess.Popen(..., start_new_session=True)`), with:
   - `VERITAS_INSTALL_ROOT=<node_root>`
   - `LD_PRELOAD=/usr/lib/x86_64-linux-gnu/libjemalloc.so.2`
   - `MALLOC_CONF=background_thread:true,dirty_decay_ms:1000,muzzy_decay_ms:0,narenas:2`
   - stdout/stderr redirected to a log file under `<node_root>` (path TBD at implementation — matching the script's `${ORG}-${NODE}.out` convention is a reasonable default)
   - Immediately record `pid = proc.pid`, `status = 'running'` on the node row.
4. **Liveness check:** brief pause (`0.25`s, matching the script), then `proc.poll()`. If the process has already exited:
   - Clear `pid`, set `status = 'stopped'`.
   - Print the failure to screen, along with the tail of the log file written in step 3 (last ~50 lines, or lines matching an error marker if `verilock`'s log format supports one).
5. **Best-effort `cp_state` refresh** (§4a) — only if step 4 succeeded (a node that failed to come up has nothing new to report). Same shape as `stop`'s (§5.3 step 1): attempt, update on success, proceed regardless of failure.

No file-staging step (the script's `stage_node_environment`) — retired by this phase. `verilock` is expected to read its identity (`client_id`, and `node_id` once assigned) and config (env URLs, Kafka properties from §2/§3) via the `--profile`-selected environment, sourced from `store.db`, rather than from `etc/veritas-lock.<env>.conf` / `identity-<env>/` staged into canonical paths. This is `verilock`-side work tracked outside this document; until it lands, `vl node start` is not expected to produce a working node.

### 5.3 `vl node stop <org> <name> [--env <env>]`

No `--as` — the only REST calls in this command's flow (the best-effort `cp_state` refresh below, and the blocking CP-DOWN-confirmation the fleet-level orchestration performs after signaling, `vl-network-spec.md` §3.2) authenticate as the node's own service account, not an operator identity — see §4a and the note in that orchestration section.

Single-node mechanics only — ordering/staggering across a fleet is `vl org stop`/`vl network stop`'s job (`vl-org-phase2-spec.md` §8, `vl-network-spec.md` §3.2), layered on top of this.

1. **Best-effort `cp_state` refresh** (§4a) — attempt a `GET /nodes?host=<node.host>&port=<port>`, update `cp_state`/`cp_state_synced_at` on success, proceed regardless of failure. Purely informational; does not affect anything below.
2. If the node row has no `pid` recorded, report "not running (or not started by `vl`)" and return — this is what preserves starting a node manually (e.g. under a debugger) and having fleet-level orchestration leave it alone.
3. If `pid` is recorded but `kill -0` shows it's not alive, clear `pid`/`status` and report done (stale bookkeeping, not an error).
4. Otherwise: send `SIGTERM`. Poll every `0.25`s (via `kill -0`) up to a `10`s timeout. Still alive after timeout → `SIGKILL`, brief pause.
5. Clear `pid`, set `status = 'stopped'`.
6. Return success/failure to the caller (used by the fleet-level orchestration docs to decide whether to proceed to the control-plane wait).

No `pgrep`-by-binary-path fallback (present in the script) — `vl` always records the `pid` it started, so a missing/stale DB `pid` *is* the signal that this node isn't `vl`'s to stop, not a case to search around.

### 5.4 `vl node reset <org> <name> [--yes] [--env <env>]`

No `--as` — like `stop`, this command's REST call (the control-plane status check in step 1) authenticates as the node's own service account, resolved via `svc_acct_id` (§4), not as an operator identity — this is "ask the CP about this specific node," which the node's own credentials are the natural authority for.

1. If `status = 'new'`, skip straight to step 2 — there's no CP-side node yet to check (it's never been started), so there's nothing to query and nothing that could be `UP`/`STANDBY` (and nothing to refresh `cp_state` from either). Otherwise, check the node's running state via the control plane (`GET /nodes?host=<node.host>&port=<port>` → `NodeDto.state`; treat anything other than `DOWN` — i.e. `UP` or `STANDBY` — as "still up"), **not** the local `status` column alone. On a successful response, this doubles as the §4a refresh — update `cp_state`/`cp_state_synced_at` from the same result, no separate call. Refuse with a clear error ("node1 is still UP — stop it first") if not `DOWN`.
2. **Confirm**, unless `--yes` was passed: prompt `Reset <org>/<name>'s data directory? This permanently deletes all local blockchain state. Are you sure (Y/n)?` — a blank/`Enter` answer or `y`/`Y` proceeds, `n`/`N` aborts with no changes made. If no TTY is attached and `--yes` wasn't passed, fail immediately with a clear message ("`vl node reset` requires confirmation; pass `--yes` to run non-interactively") rather than hanging on an unanswerable prompt — same pattern already established for the tier-2 password prompt (`vl-identity-user-spec.md` §7).
3. `rm -rf <node_root>/data && mkdir -p <node_root>/data`. Nothing else is touched — not the log file, not `bin`/`lib` (re-copied fresh on next start regardless), not `node_id` on the row (a reset node keeps its previously-assigned CP identity), not `status` (a `new` node resetting stays `new`; it still hasn't been started).

**The `(Y/n)` default is genuinely "yes on Enter,"** as written — worth double-checking that's what you actually want for a destructive, irreversible operation before this goes to implementation. `(y/N)` (default no) is the more common convention for anything that deletes data; `(Y/n)` here was specified as-is, but flagging the asymmetry in case it wasn't a deliberate choice.

---

## 6. Open Items

1. **`$HOME/orgs` root is hardcoded**, not read from config or `store.db`. Deliberate — Docker-based node execution is expected to replace this filesystem layout in the near term, and making the root configurable now is work likely to be thrown away twice (once to build it, once to remove it). Revisit only if Docker's timeline slips significantly.
2. **GLOBO-specific shutdown ordering** (org sorted last, descending `nodeN`, in the script's `SHUTDOWN_NODE_DIRS` construction) is carried over as-is by the fleet-level orchestration built on top of §5.3 (`vl-network-spec.md` §3.2, `vl-org-phase2-spec.md` §8) without re-examining whether that's still the right rule now that orchestration lives in `vl` rather than a hand-maintained script. Worth a deliberate decision later rather than silently perpetuating it — tracked here since it originates from this document's `node stop` mechanics, referenced rather than duplicated in those two documents.
3. **No `list`/`show`/`update`/`delete` for `vl node` in this phase** — only `create`/`start`/`stop`/`reset`. Following the other nouns' conventions (`vl org list`, `vl team show`, etc.), these would be natural, low-risk additions; omitted here only because they weren't part of the driving use case (rebuilding `start_test_harness.sh`'s behavior) and can be added additively without touching this document's schema.
4. **`verilock`'s `--profile`-driven, store-backed config resolution is a dependency of this document, not something it implements.** `vl node start` (§5.2) assumes `verilock` can look itself up in `store.db` given a `client_id` and the resolved `--env` name; until that lands on the `verilock` side, node startup will run but nodes won't come up correctly. Worth tracking as a blocking dependency, not a `vl`-side task.
5. **`env_kafka_property` has no seed/write path defined** — this document specifies the table and its consumption by `vl node start`, but not how rows get populated (manual `INSERT` for now? a future `vl env kafka-property set` command?). Left open since the current property set is static and known, but will need an answer before `env_kafka_property` needs to change per-environment in practice.
6. **`vl node start` has no remote-exec support in this phase** — `host` (§4) records where a node's CP status can be queried, but `start` always spawns on the machine `vl` itself runs on, regardless of what `host` holds. Setting `--host` to a genuinely remote address today would produce a node row that `stop`/`reset` query correctly but that `create`+`start` can never actually bring up. This is the known, planned gap this document leaves for a fast-follow — see §7, which lays out the direction (SSH) rather than leaving it purely open-ended.

---

## 7. Future phase: remote node execution via SSH

**Not part of this document's implementation** — captured here as the planned direction for a fast-follow phase, so the design choices already made (§4's `host` column, in particular) are made with this in mind rather than needing to be revisited when it lands.

**Goal:** run `vl node create`/`start`/`stop`/`reset` against a node hosted on a different machine than the one `vl` itself runs on, given (a) network connectivity to that machine and (b) an SSH keypair already set up for passwordless access to it — not a new authentication scheme of its own, just standard SSH.

**How this builds on what's already specced, not instead of it:**
- `host` (§4) already exists and is exactly the field that would carry a real remote address instead of `127.0.0.1` — no schema change needed to *store* the target; §6 item 6 is what's missing on the *execution* side.
- `svc_acct_id`/credentials, `port`, `node_id`, `status`, `cp_state` all stay meaningful unchanged — a remote node is still one row, still authenticates to the CP the same way, still has the same lifecycle states. Only the *mechanism* `start`/`stop` use to act on the node's process changes; nothing about what they track changes.
- The CP-facing REST calls (`stop`'s and `reset`'s status checks, §5.3–§5.4) are already host-agnostic — they ask the CP about a `(host, port)` pair over the network regardless of where `vl` itself is running, so none of that logic needs to change for remote nodes at all. Only the *local* mechanics — copying `bin`/`lib`, spawning the process, sending signals, tailing the log — are currently local-only and would need an SSH-backed equivalent.

**What a fast-follow phase would need to add (direction, not a committed design):**
- An SSH connection mechanism (likely `paramiko` or shelling out to the system `ssh`/`scp`/`rsync`) for a node whose `host` isn't `127.0.0.1`/`localhost`.
- Setup (§5.2 step 2) becomes `scp`/`rsync` of `bin`/`lib` to the remote host instead of a local `cp`.
- Spawn (§5.2 step 3) becomes an SSH-invoked, detached remote process (`ssh ... nohup ...` or equivalent) instead of `subprocess.Popen`. `pid` still gets stored the same way — it's the *remote* PID, meaningful only in combination with `host` for `stop`'s signal-sending.
- `stop`'s `SIGTERM`/`SIGKILL` (§5.3 steps 3–4) becomes an SSH-invoked `kill` against the remote PID instead of a local one.
- The liveness/log-tail steps (§5.2 step 4) need a remote file read (`ssh ... tail ...` or `scp` the log back) instead of a local file open.
- A prerequisite for any of this: how `vl` locates/verifies the SSH keypair for a given `host` is undecided — a new `node` column, a convention based on `~/.ssh/config`, or something env-scoped like the URLs in §2 are all plausible directions, not decided here.

Whether this is a `vl`-side SSH implementation or a thinner layer that just shells out to `ssh`/`scp` directly is also an open design choice for that future phase, not resolved by this note — the point of this section is scoping the eventual work and confirming today's schema doesn't need to change to support it, not specifying it.
