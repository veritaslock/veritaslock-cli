# veritaslock-cli (`vl`) — Phase 7: `vl node`

**Status:** Draft for implementation
**Part of:** the larger `vl-cli-phase1-crud-spec.md` effort, broken out as its own standalone document.
**Depends on:** `vl-env-spec.md` (Phase 1, for `environment`, including `token_url`/`schema_reg_url`/`kafka_bootstrap` — this document consumes that schema as-is, no longer extends it), `vl-service-account-spec.md` (Phase 4, for `identity`/`svc_acct` — `vl node create` creates a `NODE`-role service account via the same code path `vl svc-acct add` uses, not a parallel one), `vl-org-spec.md` (Phase 2, for the `organization` cache table `node.org_name` FKs against), `vl-org-phase2-spec.md` §8 (the org-scoped `start`/`stop`/`reset` orchestration built on top of this document's `vl node` verbs — not yet merged into `vl-org-spec.md` itself, see that document's own scope note).
**See also:** `cp-node-identity-spec.md` — the control-plane redesign (`(org_id, org_node)` identity, idempotent registration, the `(org_id, org_node)`-keyed status lookup `vl` now uses instead of `(host, port)`) this document's `org_node` column is shaped around; `verilock-node-startup-spec.md` — the `verilock`-side changes (self-reported `host`, `(org_id, org_node)` lookup at startup, config file rendering) `vl node start`/`stop`/`reset` depend on. Neither is `vl`-side work, but both directly inform what's in this document (§4b, §5.2).
**Builds on:** `start_test_harness.sh` — this phase re-implements that script's per-node start/stop/reset mechanics (spawn, kill, CP-status check) as the `vl node` verbs, using `store.db` as the source of truth for node identity and configuration instead of files staged under each node's `VERITAS_INSTALL_ROOT`. Fleet-level orchestration (the script's start/shutdown loops and their ordering/stagger) is specced separately in `vl-network-spec.md` (Phase 8) and `vl-org-phase2-spec.md` §8, both built on top of this document's verbs rather than repeating their mechanics.
**Schema baseline:** current store schema version is `6` (post `vl team` migration). This document bumps to `7`.

---

## 1. Scope

This document covers:
- A new `env_kafka_property_override` table — sparse per-environment overrides for the librdkafka consumer/producer properties that ship as a static file in the `verilock` node repo (`enable.auto.commit`, `session.timeout.ms`, etc.); schema only in this phase, not yet consumed (§6 Open Items).
- A new `node` table — one row per provisioned node, scoped to an org.
- The `vl node` command group: `create`, `start`, `stop`, `reset`, `list`, `show`. (No `update`/`delete` in this phase — see §6 Open Items.)

**Not covered here:** fleet-level orchestration. `vl org start`/`stop`/`reset` is specced in `vl-org-phase2-spec.md` §8, and `vl network start`/`stop`/`reset` in `vl-network-spec.md` (Phase 8) — both are thin orchestration layered on top of this document's `vl node` verbs, kept in their own documents so `vl org`'s and `vl network`'s command surfaces are each documented in one place.

**Explicitly out of scope:** `send_events.sh`'s migration (staying bash for now, ported to read `store.db` directly rather than hardcoded paths — separate, independent effort), and any Docker-based node runtime (the filesystem-rooted `$HOME/orgs/<org>/nodes/node<n>` layout below is understood to be transitional).

**Planned fast-follow, not part of this phase:** remote node execution via SSH — running these same verbs against a node on another machine, given network connectivity and a pre-established SSH keypair. §7 lays out the direction — note that it now needs an address field `vl` doesn't otherwise carry (§4b dropped `host` from this document's schema), since SSH administration and CP-registered runtime address turn out to be different concerns; see §7's revised note.

---

## 2. `environment` (no longer extended here)

`token_url`, `schema_reg_url`, and `kafka_bootstrap` — all `NOT NULL`, required at `vl env add` — are specced in `vl-env-spec.md` §3 as part of the base `environment` schema, not as an addition from this document. (An earlier draft of this document treated them as additive columns layered on top of a Phase 1 baseline that didn't yet have them; that's now resolved upstream, so this document just consumes the six-column URL/bootstrap set as-is: `idp_base_url`, `cp_base_url`, `di_base_url`, `token_url`, `schema_reg_url`, `kafka_bootstrap`.)

## 3. Schema: `env_kafka_property_override`

| Field | Type | Notes |
|---|---|---|
| environment_name | text | `REFERENCES environment(name) ON UPDATE CASCADE ON DELETE CASCADE` |
| key | text | e.g. `session.timeout.ms`, `auto.offset.reset` |
| value | text | |

`PRIMARY KEY (environment_name, key)` — natural uniqueness (no duplicate property per env), and an easy `INSERT OR REPLACE` upsert pattern when seeding or updating.

`ON DELETE CASCADE` here (unlike `environment`'s own `RESTRICT` against most referencing tables, §3 of `vl-env-spec.md`) — a property row has no independent meaning without its parent environment, same reasoning as `team_member`'s cascade from `team`.

**Overlay, not the full property set.** Almost all of `verilock-kafka-consumer.conf`'s properties are fixed regardless of environment and ship as a static file in the `verilock` node repo (staged to `$HOME/.veritaslock/etc` by that repo's own install target) — this table exists only for the rare property that genuinely needs to differ per environment. A row here means "override this one key's value for this one environment"; an environment with no rows uses the static file entirely as shipped.

No per-node override in this phase — sparse enough already that a per-node layer isn't warranted; if the need arises, a `node_kafka_property_override` table with the same shape, consulted first and falling back to this one, is a purely additive follow-up.

No CLI commands to manage this table in this phase — populated by seed/migration only, per the schema bump noted in the header above. **Not yet consumed by `vl node start` either** — see §6 Open Items. The table exists so the schema is ready whenever an override is actually needed, without a future migration to add it.

## 4. Schema: `node`

| Field | Type | Notes |
|---|---|---|
| id | integer | `PRIMARY KEY AUTOINCREMENT` — surrogate key |
| environment_name | text | `REFERENCES environment(name) ON UPDATE CASCADE ON DELETE RESTRICT` |
| org_name | text | `REFERENCES organization(environment_name, name)` (composite, via `environment_name` above), same pattern as `team.org_name` |
| org_node | integer | the node's number within its org — `1`, `2`, etc. Not a free-text name (see §4b): this is the same value the control plane addresses the node by (`(org_id, org_node)`, per the CP-side redesign — see `cp-node-identity-spec.md`), so `vl` and the CP always agree on what "node1" means without any parsing or translation between them |
| host | text | nullable, no default, **not populated or used by any `vl node` command in this phase**. Reserved for the planned SSH fast-follow (§7); left in the schema deliberately rather than added back later — see §4b |
| svc_acct_id | integer | `NOT NULL`, `REFERENCES svc_acct(identity_id) ON DELETE RESTRICT` — the `NODE`-role service account created alongside this row (§5.1); named for the table it points at, even though the column it targets is `svc_acct.identity_id` (that table's PK, `vl-service-account-spec.md` §2). `client_id`, and the node's keypair (`svc_acct.private_key_path`/`public_key_path`), are read via this FK, not duplicated onto `node` |
| port | integer | `NOT NULL` — auto-assigned starting at `7001`, globally unique across the *entire store* (every node, in every org and every environment — see §4b), not just within one environment; overridable via `--port` at creation |
| pid | integer | nullable — set on successful `start`, cleared on `stop`/detected-dead |
| state | text | `NOT NULL DEFAULT 'NEW'`, `CHECK (state IN ('NEW', 'RUNNING', 'STOPPED'))` |
| created_at | text | ISO8601 |

`UNIQUE (environment_name, org_name, org_node)`.

`ON DELETE RESTRICT` on `svc_acct_id` (rather than `CASCADE`, unlike most of this store's identity-owned children) — a `node` row is the more significant, longer-lived record here; deleting the underlying service account out from under an existing node should fail loudly rather than silently orphan the node's credentials. There's no `vl node delete` in this phase (§6, item 3) to make this concrete yet, but the constraint is worth setting correctly now rather than defaulting to `CASCADE` and revisiting later.

`state` is tracked as an explicit column rather than derived purely from `pid IS NOT NULL` — mildly redundant, but makes `WHERE state = 'RUNNING'` queries and CLI table output direct, and leaves room for a future state (`CRASHED`) without a schema change. `vl` is the sole writer of `pid`/`state` — no independent liveness scan reconciles them; a node started or killed outside `vl` (e.g. under a debugger, per §5.3) is simply invisible to this bookkeeping, which is the intended behavior, not a gap.

**`NEW` is distinct from `STOPPED`**, not a synonym for it: a node's control-plane counterpart doesn't exist until `vl node start` actually runs it for the first time — the CP has no row for a node that's only ever been `vl node create`d — so `STOPPED` (which implies "was running, isn't now") would be a misleading state for a node that has never been started at all. `NEW` is the only state set by `create` (§5.1); `start`'s first successful run (§5.2) is what transitions a node out of `NEW` for good, to `RUNNING`, and it never returns to `NEW` afterward — `STOPPED` (from `stop`, §5.3) is the only state a subsequent `start` finds it in. `vl node reset` (§5.4) treats `NEW` as a special case (see that section) since there's no CP-side node yet to check status against.

**`vl node start` (§5.2) never needed `host` for spawning** — spawning is always local to the machine `vl` runs on in this phase (`subprocess.Popen`, no remote-exec support). None of `vl node`'s CP status lookups (`stop`/`reset`, §5.3–§5.4) use it either — those query the CP by `(org_id, org_node)` instead, which `vl` already has locally without needing to track where `verilock` last registered from. `host` stays on the table only as a reserved, currently-unused column (§4b) — not read or written by anything in this phase.

### 4b. `org_node` and `host` — corrections from an earlier draft of this document

Two decisions in an earlier version of this schema turned out to be wrong once the control-plane side was thought through properly (see `cp-node-identity-spec.md` for the full CP redesign this section summarizes the `vl`-side consequences of):

**`host` defaulting to `127.0.0.1` was a real bug, not a placeholder.** The CP's own node table holds a real, routable IP — other nodes pull snapshots from each other over REST using exactly that address, so a same-machine-only default would silently work in dev and then either fail or connect to the wrong process the moment two nodes are ever on separate hosts. `vl` has no way to know a node's real address before that node has actually bound to a socket and discovered it for itself.

**`vl` doesn't need `host` for anything active in this phase.** The only reason `vl` needed `host` locally at all was to build the CP status query (`GET /nodes?host=&port=`, `stop`/`reset`). The same `(org_id, org_node)` addressing that fixes the CP's duplicate-row bug (`cp-node-identity-spec.md` §2) gives `vl` a strictly better way to ask the CP about a node's status — `org_id` is already sitting in the local `organization` cache (`server_org_id`), and `org_node` is a column `vl` already owns, so there's no functional need to also track `host` just to look a node up. And `vl` structurally *can't* keep it current even if it wanted to: the config handoff to `verilock` is one-directional (`vl` renders files, `verilock` reads them, §5.2) — there's no path back for `verilock` to tell `vl` what address it registered under, short of `vl` polling the CP for it, which is just the status query working backwards.

**`host` stays on the table anyway, reserved and unused, rather than being removed** — a deliberate call, not an oversight: it's earmarked for the SSH fast-follow (§7), and re-adding a dropped column later is more work than leaving an unused nullable one in place now (SQLite's `ALTER TABLE` didn't support `DROP COLUMN` until 3.35 — removing it today and re-adding it later could mean a full table-rebuild migration rather than two simple `ALTER TABLE`s). `host` still matters *to the CP* (and to other nodes, for snapshot pulls) — it just isn't data `vl` itself reads or writes in this phase.

**A free-text `--name` was incompatible with a stable server-side identity.** The original design let `vl node create` accept an arbitrary `--name` (`primary`, not just `node<N>`), with auto-numbering skipping anything that didn't fit the pattern when computing the next default. That's no longer workable: the CP-side redesign addresses a node by `(org_id, org_node)` — a numeric index — specifically to fix the bug where the same logical node re-registered as a brand-new CP row every time it started from a different host (see `cp-node-identity-spec.md` for why). A node named `primary` has no number to give the CP. The `org_node` column replaces free-text `name` outright, and `--node <n>` (an integer override, same shape as `--port <n>`) replaces `--name`.

**Port allocation stays environment-agnostic, but for a corrected reason.** `MAX(port)+1` across the *entire store* — not scoped to one environment — because port uniqueness is protecting a physical, single-machine resource (`store.db` describes exactly one host, per its own "local-only" design), and two nodes from *different* environments can absolutely be started concurrently on that one machine. Node *identity*, by contrast, stays scoped per-`(environment_name, org_name)` — `local`'s GLOBO node1 and `dev`'s GLOBO node1 are legitimately two different rows with two different service accounts, since a service account is credentials against one specific IdP instance and there's no version of "shared identity" that doesn't also require sharing credentials across IdPs that don't know about each other. Only the port check is store-wide; everything else about a node's identity stays environment-scoped as originally designed.

One acknowledged, accepted gap, carried over from the same principle the spec already applies to an explicit `--port` override (§5.1 step 2): `store.db` only has visibility into nodes *it* created. If a genuinely different machine runs its own `vl` against its own `store.db` for the same environment, neither store can see the other's port assignments — auto-assignment is a same-machine convenience, not a distributed guarantee, and the real protection against two nodes colliding at the same reachable `host:port` lives entirely in the CP's `(org_id, org_node)` + `409`-if-not-`DOWN` check (`cp-node-identity-spec.md`), not in anything `vl` does locally.

---

## 5. Commands: `vl node`

Every verb below also accepts `--help` explicitly (in addition to the existing "missing required argument prints help" behavior, `vl-command-restructure.md` §2) — a user can request it directly rather than only getting it by omitting something.

### 5.1 `vl node create <org> [--node <n>] [--port <n>] [--as <label>] [--env <env>]`

`<org>` positional, not `--org` — matching `start`/`stop`/`reset` (§5.2–§5.4, which already take `<org> <n>` positionally) and `vl team add <org> <name>` (`vl-team-spec.md` §5.1) elsewhere in this project. An earlier draft of this command used `--org <org>` as a required option, inconsistent with both.

No `--host` — per §4b, `vl` doesn't track `host` for any node in this phase; the column stays `NULL` on every row `create` inserts.

1. Resolve the node's number: if `--node` is omitted, one greater than `MAX(org_node)` for this org (including deleted nodes — gaps from deletion are not reused, and avoids confusion if a deleted node's old directory/logs are still on disk). `--node` accepts any positive integer as an override, same shape as `--port`.
2. Resolve `<port>`: if omitted, one greater than `MAX(port)` across the entire store — every node, in every org and every environment (§4b) — starting from `7001` if none exist yet. Explicit `--port` overrides and is not validated for uniqueness in this phase — an operator-supplied collision is the operator's problem, same principle as everywhere else `vl` doesn't pre-check what the server (or, here, the OS) will reject anyway.
3. **Check `$HOME/orgs/<org>/nodes/node<n>` doesn't already exist.** If it does, abort immediately — no service account created, no `node` row, nothing touched — with a clear error (`$HOME/orgs/<org>/nodes/node<n> already exists — remove it manually before creating this node. It may hold stale identity/data from a previous incarnation of this node (e.g. after a control-plane reset); deleting it here automatically, silently or otherwise, risks removing something the operator didn't mean to lose, so this is left to a deliberate manual step rather than any kind of automatic cleanup.`). This check runs *before* step 4, specifically so a collision here can never leave an orphaned service account behind with no corresponding node — the same "any failure leaves nothing behind" guarantee step 4 already promises on its own account, just extended to cover this failure mode too.
4. Create a `NODE`-role service account, calling the same underlying function `vl svc-acct add` uses (not a parallel implementation) — if this step fails, abort with no `node` row and no directory created.
5. Create `$HOME/orgs/<org>/nodes/node<n>` on disk (hardcoded root for this phase — see §6 Open Items; not read from config or the store). Step 3 already guarantees this can't collide with an existing directory.
6. Insert the `node` row: `svc_acct_id` from step 4's resulting `svc_acct` row, `org_node` from step 1, `host = NULL`, `pid = NULL`, `state = 'NEW'`.

No caller-kind pre-check beyond what step 3's underlying service-account creation already enforces — same governing principle as `vl team add` (`vl-team-spec.md` §5.1) and `vl svc-acct add`'s own precedent.

### 5.2 `vl node start <org> <n> [--env <env>]`

No `--as` — nothing in this command calls the CP or IdP; it's a local spawn plus local `store.db` writes. (`create`, §5.1, is the only `vl node` verb that still takes `--as`, since it's the only one that calls the IdP as an operator.)

No `--profile` flag, and `verilock` is spawned with no arguments at all — an earlier draft of this document assumed `verilock` needed to be told which environment to read via `--profile`, on the assumption that it would pull its own config from `store.db` given that hint. Since config is instead fully rendered to files *before* `verilock` ever starts (step 2 below), there's nothing left for a `--profile` flag to select — `verilock` just reads whatever `vl` already wrote for the `--env` this `start` invocation targeted. `--env` itself still defaults the normal way (§6 of `vl-env-spec.md` — the store's default environment if omitted).

1. **Pre-check:** if the node's `state = 'RUNNING'` and `pid` is alive (`kill -0`), print `node1 is already running (pid <pid>)` to stdout and return normally — **not** a raised error, at either the single-node CLI level or the underlying function level. This distinction matters beyond just this one command's own exit code: `vl org start` (`vl-org-phase2-spec.md` §8) calls this same per-node logic directly, in a loop, and relies on it *not* raising here so one already-running node doesn't abort the rest of the batch — see that document's own note on this. `vl network start` (`vl-network-spec.md` §3.1) inherits this in turn, one layer up, by calling `vl org start` per org rather than this command directly. `verilock` also refuses to double-start on its own, but checking first here avoids the wasted setup/spawn/log-parse cycle below and gives a clearer message.
2. **Setup** (idempotent — re-run unconditionally on every start, matching current script behavior):
   - Copy `$HOME/.veritaslock/bin/*` → `<node_root>/bin`, `chmod +x`.
   - Copy `$HOME/.veritaslock/lib/liblog4cplus.so*` → `<node_root>/lib` (in-tree dependency, not a system package — `verilock` aborts on startup without it).
   - Copy `$HOME/.veritaslock/etc/log4cplus.properties` → `<node_root>/etc`, verbatim.
   - Copy `$HOME/.veritaslock/etc/verilock-kafka-consumer.conf` → `<node_root>/etc`, verbatim — no per-environment overlay applied in this phase (`env_kafka_property_override`, §3, is schema-only for now).
   - `study-samples` is **not** copied per node — `verilock` reads it directly from `$HOME/.veritaslock/etc/study-samples` in place, same as `start_test_harness.sh` does today, since its contents don't differ per node.
   - Write `client_id` (from the node's `svc_acct` row) as a flat file to `<node_root>/identity/client_id` — not part of `veritas-lock.conf`. `client_secret` is **not** written here at all: the node authenticates to the IdP via a private-key-signed JWT assertion (the same mechanism `vl svc-acct get-assertion` uses, `vl-service-account-spec.md` §5.7), not a client secret, so there's no secret for `verilock` to read.
   - Symlink (not copy) the node's own keypair — already generated at `vl node create` time via the same underlying logic `vl svc-acct add` uses (`vl-service-account-spec.md` §4 step 6), at `svc_acct.private_key_path`/`public_key_path` (`~/.config/vl/keys/<client_id>/{private,public}.key`) — to `<node_root>/identity/private.key` and `<node_root>/identity/public.key`. Symlinked, not copied, specifically to support key rotation transparently: overwriting the contents of the canonical file under `~/.config/vl/keys/<client_id>/` (a future rotate-keys command, not yet specced) is picked up by `verilock` the next time it signs an assertion, with no need to re-run `vl node start` to refresh a stale copy.
   - `vl` writes/symlinks exactly these three entries (`client_id` as a flat file, `private.key`/`public.key` as symlinks) into `<node_root>/identity/` on every start and touches nothing else there — `node.id` is `verilock`'s own. `metadata.json` (a display-name/description/role file the old provisioning script used to write, purely as human-readable documentation — never read by `verilock` itself) is deliberately **not** written by `vl` in this phase: that same information already lives in `store.db` via the node's `svc_acct` row, so a separate static file would just be a second, driftable copy of it. Revisit only if something's found to actually depend on the file existing.
   - Render `veritas-lock.conf` (env URLs, `org.node`, `port`) to `<node_root>/etc`, fresh on every start, fully from `store.db` — this is the step that makes `--profile` unnecessary (see above), and the only one of these files `vl` generates rather than copies. `org.node`'s value comes from the node's `org_node` column (§4) — the `.`-separated property name is just this file's own naming convention, matching every other property in it; it isn't a schema or column rename. `org_id` is deliberately not part of this file — `verilock` gets it from the JWT claim when it authenticates to the IdP, not from config (`verilock-node-startup-spec.md` §2.1). Full file format detail in `verilock-node-startup-spec.md` §3, not repeated here.
3. **Spawn:** `<node_root>/bin/verilock`, detached into its own session (Python equivalent of the script's `setsid`: `subprocess.Popen(..., start_new_session=True)`), with:
   - `VERITAS_INSTALL_ROOT=<node_root>`
   - `LD_PRELOAD=/usr/lib/x86_64-linux-gnu/libjemalloc.so.2`
   - `MALLOC_CONF=background_thread:true,dirty_decay_ms:1000,muzzy_decay_ms:0,narenas:2`
   - stdout/stderr redirected to `$HOME/tmp/<org>-node<n>.out`, matching `start_test_harness.sh`'s existing convention exactly (same file, same location — not under `<node_root>`)
   - Immediately record `pid = proc.pid`, `state = 'RUNNING'` on the node row.
4. **Liveness check:** brief pause (`1`s — long enough to cover a real network round trip if `verilock`'s startup fails on the CP side, e.g. a rejected registration; the script's original `0.25`s was sized for catching only an immediate crash like a bad path or missing library, and was too short to catch a CP-side rejection in practice), then `proc.poll()`. If the process is still alive, print `Started <org>/node<n> (pid <pid>)` to stdout — the success confirmation, mirroring `stop`'s own (§5.3 step 4). If it has already exited instead:
   - Clear `pid`, set `state = 'STOPPED'`.
   - Print the failure to screen, along with the tail of the log file written in step 3 (last ~50 lines, or lines matching an error marker if `verilock`'s log format supports one).

No separate file-staging step beyond step 2 above (the script's `stage_node_environment` is fully retired) — `verilock`'s own file-reading logic needs no change at all; only what populates those files changes, from hand-maintained `*.local.conf`/`*.server.conf` variants to `vl`-rendered content sourced from `store.db`. Full detail, including `verilock`'s own new startup responsibilities (looking itself up and self-reporting its `host` at the CP by `(org_id, org_node)`, via a single idempotent registration call, rather than the old unconditional `POST`-creates-a-new-row behavior), is in `verilock-node-startup-spec.md` — `cp-node-identity-spec.md` covers the CP-side half of that fix.

### 5.3 `vl node stop <org> <n> [--env <env>]`

No `--as`, and no REST calls at all — this command is entirely local (signal + `store.db` writes). The blocking CP-DOWN-confirmation poll happens at the fleet-orchestration layer, after `stop` returns, not here — see `vl-org-phase2-spec.md` §8.

Single-node mechanics only — ordering/staggering across a fleet is `vl org stop`'s job (`vl-org-phase2-spec.md` §8), layered on top of this; `vl network stop` (`vl-network-spec.md` §3.2) calls that per org rather than implementing its own copy.

1. If the node row has no `pid` recorded, print "not running (or not started by `vl`)" to stdout and return normally — this is what preserves starting a node manually (e.g. under a debugger) and having fleet-level orchestration leave it alone. **Not a raised error** — same reasoning as `start`'s pre-check (§5.2): `vl org stop` (`vl-org-phase2-spec.md` §8) calls this per-node logic directly in a loop, and a node that's already stopped (or never `vl`'s to begin with) shouldn't abort the rest of the batch. `vl network stop` inherits this one layer up, by calling `vl org stop` per org.
2. If `pid` is recorded but `kill -0` shows it's not alive, clear `pid`/`state` and print done to stdout — stale bookkeeping, not an error, same non-raising treatment as step 1.
3. Otherwise: send `SIGTERM`. Poll every `0.25`s (via `kill -0`) up to a `10`s timeout. Still alive after timeout → `SIGKILL`, brief pause.
4. Clear `pid`, set `state = 'STOPPED'`, and print `Stopped <org>/node<n> (pid <pid>)` to stdout — the only success message in this command that names the org/node/pid explicitly; steps 1–2's no-op messages above already identify what happened without needing this one repeated.
5. Return success/failure to the caller (used by the fleet-level orchestration docs to decide whether to proceed to the control-plane wait).

No `pgrep`-by-binary-path fallback (present in the script) — `vl` always records the `pid` it started, so a missing/stale DB `pid` *is* the signal that this node isn't `vl`'s to stop, not a case to search around.

### 5.4 `vl node reset <org> <n> [--yes] [--env <env>]`

No `--as` — this command's one class of REST call (the control-plane check in step 2) authenticates as the node's own service account, resolved via `svc_acct_id` (§4), not as an operator identity — this is "ask the CP about this specific node," which the node's own credentials are the natural authority for.

**Doesn't stop the node itself.** An earlier draft of this section had `reset` auto-stop a running node before deleting its data, reusing `stop`'s `SIGTERM`/poll/`SIGKILL` mechanics inline — but that meant duplicating `stop`'s poll-until-`DOWN`/stagger logic (`vl-org-phase2-spec.md` §8) a second time, inside a different command, for behavior `stop` already owns correctly. The node needs to already be `DOWN` before this command does anything — via `vl node stop` directly, or via `vl org reset` (`vl-org-phase2-spec.md` §8), which explicitly stops-and-verifies each node itself before calling this command, the same way `org stop` already does — rather than this command growing its own copy of that machinery to do it implicitly. `vl network reset` (`vl-network-spec.md` §3.3) is different again: it verifies the *whole environment* is down via one bulk admin-authenticated check before calling `vl org reset` per org at all, rather than relying on each org's own per-node verification as its only guarantee.

1. **Confirm**, unless `--yes` was passed: prompt `Reset <org>/node<n>'s data directory? This permanently deletes all local blockchain state. Are you sure (y/N)?` — only `y`/`Y` proceeds; a blank/`Enter` answer, `n`/`N`, or anything else aborts with no changes made. If no TTY is attached and `--yes` wasn't passed, fail immediately with a clear message ("`vl node reset` requires confirmation; pass `--yes` to run non-interactively") rather than hanging on an unanswerable prompt — same pattern already established for the tier-2 password prompt (`vl-identity-user-spec.md` §7).
2. If `state = 'NEW'`, skip straight to step 3 — there's no CP-side node yet to check (it's never been started). Otherwise, **a single check** via the control plane (`GET /nodes?orgNode=`, authenticated as this node's own service account — `org_id` comes from that JWT, not a query parameter — → `NodeDto.state`, `cp-node-identity-spec.md` §2.4) — **not a poll, no retry, no timeout of its own**: if it isn't `DOWN` right now, print `{org_name}/node{org_node} is still reported UP by the control plane — stop it first or try again.` and **skip** (return normally, not raise — same non-raising treatment as `start`/`stop`'s own pre-checks, §5.2–§5.3, so a batch caller like `org reset` isn't aborted by one node that isn't ready) rather than deleting anything. The two ways this can legitimately happen — a node genuinely running outside `vl`'s tracking (needs a human), or this check simply running a moment before the CP has caught up on a very recent `stop` (needs a retry, or is `org`/`network reset`'s problem to handle correctly in the first place, not this command's) — both point the same direction: this command staying simple and pushing the "make sure it's actually down first" responsibility onto whatever already-correct mechanism stops nodes, rather than this command re-deriving it.
3. `rm -rf <node_root>/data && mkdir -p <node_root>/data`, then print `Deleted <org>/node<n>'s data directory` to stdout. Nothing else is touched — not the log file, not `bin`/`lib` (re-copied fresh on next start regardless), not `state` (a `NEW` node resetting stays `NEW`; a `STOPPED` node stays `STOPPED`).

### 5.5 `vl node list [--org <org>] [--env <env>]`

Local-only, no network calls — reads directly from `store.db`'s `node` table. Unlike `vl svc-acct list`, there's no server-side "node" resource for `vl` itself to query here: the CP's own node registry is populated by `verilock` at runtime (`verilock-node-startup-spec.md` §2.2), not by `vl`, and this command doesn't touch it — the one place `vl` does talk to the CP about a node's status is `reset`'s check (§5.4 step 2), which is unrelated to listing.

Without `--org`, lists every node in the target environment across all orgs; with `--org`, narrows to that org's nodes. Columns: `org`, `node` (`org_node`), `port`, `state`, `pid`, `created_at`. Rendered via `vl.lib.output`, same convention as `vl env list` (`vl-env-spec.md` §5.2).

### 5.6 `vl node show <org> <n> [--env <env>]`

Local-only, no network calls. Detail view of a single node row: `org_node`, `port`, `pid`, `state`, `created_at`, plus identity info resolved via the `svc_acct_id` FK (§4) — `client_id` and the keypair paths (`private_key_path`/`public_key_path`). Nothing to mask: the node doesn't use `client_secret` at all (§5.2's identity-file write step only ever wrote `client_id` and the keypair, never a secret), so there's no secret-bearing field this command needs to hide behind a `--reveal-secret`-style flag the way `vl svc-acct show` does. Anyone who needs the underlying service account's own secret can get it from `vl svc-acct show <label>` directly — this command doesn't surface that label, only `svc_acct_id`, so that's a lookup by `svc_acct_id`/`client_id`, not something `vl node show` hands you directly.

---

## 6. Open Items

1. **`$HOME/orgs` root is hardcoded**, not read from config or `store.db`. Deliberate — Docker-based node execution is expected to replace this filesystem layout in the near term, and making the root configurable now is work likely to be thrown away twice (once to build it, once to remove it). Revisit only if Docker's timeline slips significantly.
2. **GLOBO-specific shutdown ordering** (org sorted last, descending `nodeN`, in the script's `SHUTDOWN_NODE_DIRS` construction) is carried over as-is by the fleet-level orchestration built on top of §5.3 (`vl-org-phase2-spec.md` §8, called per org by `vl-network-spec.md` §3.2/§3.0) without re-examining whether that's still the right rule now that orchestration lives in `vl` rather than a hand-maintained script. Worth a deliberate decision later rather than silently perpetuating it — tracked here since it originates from this document's `node stop` mechanics, referenced rather than duplicated in those two documents.
3. ~~No `list`/`show`/`update`/`delete` for `vl node` in this phase~~ — **Partially resolved:** `list`/`show` added (§5.5, §5.6). `update`/`delete` remain deferred — neither was part of the driving use case (rebuilding `start_test_harness.sh`'s behavior), and both are natural, low-risk additions later, following the other nouns' conventions (`vl org update`, `vl svc-acct delete` — though note `vl org` itself has no `delete`, per that document's own deferral), whenever there's an actual need.
4. **`vl node start`'s config file rendering (§5.2) is a dependency of this document, not something fully specified here** — the mechanics of *which* files get written and their exact format are detailed in `verilock-node-startup-spec.md`, not repeated in this document. Until that lands, `vl node start` is not expected to produce a working node.
5. **`env_kafka_property_override` has no seed/write path, and no read path either** — the table (§3) exists so the schema is ready, but nothing populates it (manual `INSERT` for now? a future `vl env kafka-property set` command?) and `vl node start`'s copy step (§5.2) doesn't apply it. Deferred because no property currently needs to differ per environment; when one does, §5.2's copy step needs to be extended to overlay matching rows onto the copied file after step 2's verbatim copy.
6. **`vl node start` has no remote-exec support in this phase** — `start` always spawns on the machine `vl` itself runs on. This is the known, planned gap this document leaves for a fast-follow — see §7, which lays out the direction (SSH) and a real open question about what `host` (§4) actually means once that phase needs it.

---

## 7. Future phase: remote node execution via SSH

**Not part of this document's implementation** — captured here as the planned direction for a fast-follow phase.

**Goal:** run `vl node create`/`start`/`stop`/`reset` against a node hosted on a different machine than the one `vl` itself runs on, given (a) network connectivity to that machine and (b) an SSH keypair already set up for passwordless access to it — not a new authentication scheme of its own, just standard SSH.

**Open question this fast-follow will need to resolve:** `host` (§4) already exists on the schema, kept deliberately unused rather than removed (§4b), but its semantics for *this* purpose still need deciding, not assumed. The CP-registered runtime address (self-reported by `verilock` at startup, tracked only by the CP in this phase) and "the address `vl` should SSH into to administer this node" aren't guaranteed to be the same value even in principle — an operator might want SSH over a different interface, a jump host, a hostname alias not known to `verilock` at all. Two real options when this phase is scoped: (a) repurpose `host` as an operator-supplied field at `vl node create` time (an `--ssh-host` at creation, rather than anything self-reported), or (b) leave `host` reserved for a future CP-address-mirroring use and add a distinct column for the SSH target. Not resolved here — flagged so it isn't assumed to be a solved problem just because the column already exists.

**How this builds on what's already specced, not instead of it:**
- `svc_acct_id`/credentials, `port`, `state` all stay meaningful unchanged — a remote node is still one row, still authenticates to the CP the same way, still has the same lifecycle states. Only the *mechanism* `start`/`stop` use to act on the node's process changes; nothing about what they track changes.
- The CP-facing REST calls (`stop`'s and `reset`'s status checks, §5.3–§5.4) are already keyed by `(org_id, org_node)`, not by any address `vl` supplies — so none of that logic needs to change for remote nodes at all, regardless of how the open question above gets resolved. Only the *local* mechanics — copying `bin`/`lib`, spawning the process, sending signals, tailing the log — are currently local-only and would need an SSH-backed equivalent.

**What a fast-follow phase would need to add (direction, not a committed design):**
- Resolution of the open question above, plus a decision on where the SSH keypair for the resulting target is located/verified — a new `node` column, a convention based on `~/.ssh/config`, or something env-scoped like the URLs in §2 are all plausible directions, not decided here.
- An SSH connection mechanism (likely `paramiko` or shelling out to the system `ssh`/`scp`/`rsync`) for a node running on a machine other than the one `vl` itself runs on.
- Setup (§5.2 step 2) becomes `scp`/`rsync` of `bin`/`lib` to the remote host instead of a local `cp`.
- Spawn (§5.2 step 3) becomes an SSH-invoked, detached remote process (`ssh ... nohup ...` or equivalent) instead of `subprocess.Popen`. `pid` still gets stored the same way — it's the *remote* PID, meaningful only in combination with the resolved target address for `stop`'s signal-sending.
- `stop`'s `SIGTERM`/`SIGKILL` (§5.3 steps 3–4) becomes an SSH-invoked `kill` against the remote PID instead of a local one.
- The liveness/log-tail steps (§5.2 step 4) need a remote file read (`ssh ... tail ...` or `scp` the log back) instead of a local file open.

Whether this is a `vl`-side SSH implementation or a thinner layer that just shells out to `ssh`/`scp` directly is also an open design choice for that future phase, not resolved by this note — the point of this section is scoping the eventual work, not specifying it.
