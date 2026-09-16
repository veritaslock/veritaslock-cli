# veritaslock-cli (`vl`) — Phase 8: `vl network`

**Status:** Draft for implementation
**Part of:** the larger `vl-cli-phase1-crud-spec.md` effort, broken out as its own standalone document.
**Depends on:** `vl-node-spec.md` (Phase 7, for the `node` table and the `vl node create`/`start`/`stop`/`reset` verbs this document orchestrates — no new schema is introduced here), `vl-org-spec.md` (Phase 2, for the `organization` cache table this document queries to resolve a network's membership), `vl-org-phase2-spec.md` §8 (the org-scoped sibling of this document's orchestration — not yet merged into `vl-org-spec.md` itself).
**Builds on:** `start_test_harness.sh` — `vl network start` ≈ its start loop, `vl network stop` ≈ its shutdown loop (stagger and CP-status-wait included), `vl network reset` ≈ its `--reset` flag. See `vl-node-spec.md` for the per-node mechanics (spawn, kill, CP-status check) this document layers on top of.
**Schema baseline:** current store schema version is `7` (post `vl node` migration, per that document's header). This document is additive to command surface only — no schema change, no version bump.

---

## 1. Scope

This document covers the `vl network` command group — a new noun, with no backing table — and its three verbs: `start`, `stop`, `reset`. Each is thin orchestration over the same-named `vl node` verb (`vl-node-spec.md` §5.2–§5.4), applied across every node in an environment.

**Not covered here:** `vl org start`/`stop`/`reset` — the org-scoped instance of this same pattern, specced in `vl-org-phase2-spec.md` §8 rather than here, since `vl org`'s full command surface already lives in that document. The two are siblings, not a hierarchy call-wise (see §3 below) — this document doesn't depend on that one's commands, only on the same underlying `vl node` verbs and the same `organization` table both use to resolve scope.

**Remote node execution (SSH):** not designed here at all — this document's orchestration is entirely "call the same-named `vl node` verb per node," so whatever mechanism `vl node start`/`stop` eventually use to reach a remote node (`vl-node-spec.md` §7, planned fast-follow) applies transparently at this level too. Nothing in this document needs to change for that to work.

---

## 2. `network` is not a table

A network is simply *all orgs in an environment* — `vl network <verb>` resolves its scope by querying `organization` for the target `environment_name`, with no dedicated `network` row or join table. This was considered against modeling `network` as a first-class table and rejected: every org in an env is a network member today, with no case for excluding one, so a table would carry a permanent 1:1 shadow of `organization` for no behavioral payoff. If a future need arises to exclude a specific org from `vl network` operations (a partially-provisioned org, one intentionally left down), the cheaper fix is an `active`/`inactive` flag on `organization` itself, not a new table.

---

## 3. Commands: `vl network` — `start` / `stop` / `reset`

Thin orchestration over `vl-node-spec.md` §5, scoped to every org in the environment via §2's resolution above (no org filter — that's `vl org start`/`stop`/`reset`, `vl-org-phase2-spec.md` §8, the same pattern one level down). The two are siblings, not a hierarchy call-wise: `vl network stop` does not shell out to `vl org stop` per org, because its ordering (below) is a single sequence across the *whole* environment's nodes, not independent per-org sequences run back to back.

All three verbs also accept `--help` explicitly, same as every `vl node` command (`vl-node-spec.md` §5).

### 3.1 `vl network start [--env <env>]`

No `--as` — every underlying call is either local (`vl node start`) or, per §4, authenticated as each node's own service account. For every node in the environment, across all orgs, order not significant: call `vl node start` (`vl-node-spec.md` §5.2). No stagger — the script's startup loop has none either.

### 3.2 `vl network stop [--env <env>]`

No `--as` — see §4. Ordering matters here, mirroring the script's shutdown loop exactly:

1. Determine shutdown order across the *entire environment's* nodes: reverse of startup order. *(The script's specific GLOBO-last, descending-nodeN-within-that ordering is preserved as-is for this phase — it encodes an operational assumption about GLOBO's role that this document doesn't re-litigate; flagged in §5 as worth revisiting if that assumption changes.)*
2. For each node in order:
   - Call `vl node stop` (`vl-node-spec.md` §5.3). If it reports "not running" (no `pid` recorded — e.g. a debugger-started node `vl` never tracked), skip silently and move to the next node, preserving the script's "leave nodes I didn't start alone" behavior.
   - After the process is confirmed exited (§5.3 step 3 of that document already blocks on this), poll the control plane (`GET /nodes?orgId=&orgNode=`, `cp-node-identity-spec.md` §2.4, same endpoint as `vl-node-spec.md` §5.4) until `state = DOWN`, so the stagger pause below doesn't start before the CP has actually ingested the `NodeDown` event. **This poll authenticates as the node being polled's own service account** (resolved via that node's `svc_acct_id`, `vl-node-spec.md` §4) — not the operator running `vl network stop` — same as `vl node reset`'s CP check (§4 below).
   - Pause: `1`s between nodes, `3`s before the last node in the whole environment stops (the "last node standing" needs longer to have observed everyone else's departure before nobody's left to observe anything further). Both overridable — matching `NODE_SHUTDOWN_STAGGER_SECS`/`NODE_SHUTDOWN_FINAL_STAGGER_SECS` env vars in the script, or a `vl`-native flag equivalent.

Uses the `NodeDto` REST endpoint, keyed by `(org_id, org_node)` (`GET /nodes?orgId=&orgNode=`, `cp-node-identity-spec.md` §2.4) rather than the old `(host, port)`-keyed lookup or the script's direct MySQL query against the control plane's database — removes a fragile cross-service DB coupling in favor of a real API, and matches how the CP now addresses a node's identity everywhere else (§2 of that document).

`vl org stop` (`vl-org-phase2-spec.md` §8) uses this same per-node mechanism and stagger logic, but sequences only that org's nodes — a genuinely different (shorter, org-scoped) ordering, not a decomposition of this command.

### 3.3 `vl network reset [--yes] [--env <env>]`

No `--as` — see §4. Confirms once for the whole batch, not once per node: prompts `Reset ALL nodes in environment '<env>'? This permanently deletes local blockchain state for every node. Are you sure (Y/n)?` (same semantics as `vl node reset`'s prompt, `vl-node-spec.md` §5.4 step 2, including the no-TTY-requires-`--yes` behavior) unless `--yes` is passed. On confirmation, calls each node's reset logic directly (not the `vl node reset` CLI command) with its own prompt suppressed — an operator who confirmed "reset everything" shouldn't be asked to confirm again per node. For every node in the environment, across all orgs: apply `vl node reset`'s steps (`vl-node-spec.md` §5.4). No ordering, no stagger — each node's data directory is independent, and unlike shutdown there's no cross-node consumption concern to sequence around.

---

## 4. Authentication

Unlike every other post-Phase-3 command group in this project, none of `vl network`'s three verbs take `--as`. Nothing here requires an operator identity: `start` is purely local orchestration over `vl node start` (itself local, `vl-node-spec.md` §5.2), and the one class of REST call these verbs make — the control-plane status check that `stop` (§3.2) and `vl node reset` (`vl-node-spec.md` §5.4) both perform — is scoped to a single node's own status, so it authenticates as that node's own service account (resolved via its `svc_acct_id`) rather than as whoever is running `vl network stop`/`reset`. This mirrors why `vl node stop`/`vl node reset` themselves dropped `--as` (`vl-node-spec.md` §5.3–§5.4): an operator identity is only needed where a command asks the IdP to do something on the operator's behalf (as `vl node create` does), not for a node checking in on itself.

---

## 5. Open Items

1. **GLOBO-specific shutdown ordering** (org sorted last, descending `nodeN`, in the script's `SHUTDOWN_NODE_DIRS` construction) is carried over as-is in §3.2 without re-examining whether that's still the right rule now that orchestration lives in `vl` rather than a hand-maintained script. Worth a deliberate decision later rather than silently perpetuating it — same open item as `vl-node-spec.md` §8 item 2, tracked once there and referenced here rather than duplicated.
2. **No `vl network list`/`show`** — since a network has no backing row, there's nothing to list/show beyond what `vl org list` and `vl node` commands already surface per org. Flagging in case a future convenience view (e.g. a single "everything in this environment" status table spanning orgs and nodes) is wanted later; not part of this phase.
