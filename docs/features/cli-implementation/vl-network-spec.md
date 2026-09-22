# veritaslock-cli (`vl`) — Phase 8: `vl network`

**Status:** Draft for implementation
**Part of:** the larger `vl-cli-phase1-crud-spec.md` effort, broken out as its own standalone document.
**Depends on:** `vl-node-spec.md` (Phase 7, for the `node` table and the `vl node create`/`start`/`stop`/`reset` verbs — orchestrated directly by `vl org`, one level down, not by this document), `vl-org-spec.md` (Phase 2, for the `organization` cache table, including `created_at`, §3, used to determine org iteration order below), `vl-org-phase2-spec.md` §8 (`vl org start`/`stop`/`reset` — this document's actual orchestration target; `vl network` calls these per org, not `vl node` per node, per §3's redesign).
**Builds on:** `start_test_harness.sh` — `vl network start` ≈ its start loop, `vl network stop` ≈ its shutdown loop (stagger and CP-status-wait included, via `vl org start`/`stop`), `vl network reset` ≈ its `--reset` flag. Per-node mechanics (spawn, kill, CP-status check, stagger) live in `vl-org-phase2-spec.md` §8, which this document reuses rather than restates — see `vl-node-spec.md` for the mechanics one level further down still.
**Schema baseline:** current store schema version is `7` (post `vl node` migration, per that document's header). This document is additive to command surface only — no schema change, no version bump.

---

## 1. Scope

This document covers the `vl network` command group — a new noun, with no backing table — and its four verbs: `start`, `stop`, `reset`, `status`. `start`/`stop`/`reset` are thin orchestration over the same-named `vl org` verb (`vl-org-phase2-spec.md` §8), applied across every org in an environment (§3.0); `status` (§3.4) is different — a read-only, admin-authenticated view with no `vl org`/`vl node` equivalent, since no single node's own credentials can answer "what's everyone's state" the way they can answer "am I down."

**Not covered here:** `vl org start`/`stop`/`reset` — the org-scoped instance of this same pattern, specced in `vl-org-phase2-spec.md` §8 rather than here, since `vl org`'s full command surface already lives in that document. This document's own `start`/`stop`/`reset` are now built directly on those org-level commands (§3) — `vl-org-phase2-spec.md` §8 is a real dependency, not a separate, independent implementation of the same idea.

**Remote node execution (SSH):** not designed here at all — this document's orchestration bottoms out at `vl org start`/`stop`/`reset` (§3), which themselves bottom out at `vl node start`/`stop` per node, so whatever mechanism `vl node start`/`stop` eventually use to reach a remote node (`vl-node-spec.md` §7, planned fast-follow) applies transparently at every level above it, including this one. Nothing in this document needs to change for that to work.

---

## 2. `network` is not a table

A network is simply *all orgs in an environment* — `vl network <verb>` resolves its scope by querying `organization` for the target `environment_name`, with no dedicated `network` row or join table. This was considered against modeling `network` as a first-class table and rejected: every org in an env is a network member today, with no case for excluding one, so a table would carry a permanent 1:1 shadow of `organization` for no behavioral payoff. If a future need arises to exclude a specific org from `vl network` operations (a partially-provisioned org, one intentionally left down), the cheaper fix is an `active`/`inactive` flag on `organization` itself, not a new table.

---

## 3. Commands: `vl network` — `start` / `stop` / `reset` / `status`

Thin orchestration over `vl org start`/`stop`/`reset` (`vl-org-phase2-spec.md` §8) — genuinely a hierarchy now, not siblings: `vl network` determines which orgs, in what order, and calls the org-level command for each; it never talks to a node directly, and doesn't restate any of `org`'s own per-node stop/poll/pause mechanics. An earlier draft of this document had `network` orchestrate nodes directly instead, reasoning that its whole-environment ordering was a "single sequence," not independent per-org sequences — but that just duplicated `org`'s already-correct, already-tested per-node logic a second time, at greater risk of the two implementations drifting apart later. `org`'s own logic (`vl-org-phase2-spec.md` §8) is unchanged by this document — it was already correct.

All four verbs also accept `--help` explicitly, same as every `vl node` command (`vl-node-spec.md` §5).

### 3.0 Determining org order

Every verb below needs to know "which orgs, in what order" — computed once, the same way, then used forwards (`start`) or reversed (`stop`, `reset`):

1. Read every org in the target environment from the local `organization` cache (`vl-org-spec.md` §3), **excluding `veritaslock`** — this is the special platform-wide org holding `PLATFORM_ADMIN` and other cross-org roles (`vl-org-spec.md` §4.5's note on `PLATFORM_ADMIN`), never a node-hosting org, and never will be; including it here would mean calling `vl org start`/`stop`/`reset` against an org that's guaranteed to have zero nodes, which is harmless but pointless. Sort what remains by `created_at` ascending — oldest first, i.e. creation order. Purely local; no network call just to determine ordering.
2. **Verify GLOBO is first in that list.** In practice it always is, since it's conventionally the first org created — but if it somehow isn't (unusual test data, a re-seeded environment), move it to the front rather than trusting creation order blindly here. GLOBO's position is what actually matters operationally (§3.1's ordering rationale, carried over unchanged); creation order is just the proxy that happens to produce it correctly in the normal case.
3. `start` uses this order as-is (GLOBO first). `stop`/`reset` use it reversed (GLOBO last) — note that reversing a GLOBO-first list always puts GLOBO last regardless of how the other orgs sort relative to each other, so nothing further is needed to guarantee that.

Non-GLOBO orgs' relative order among themselves has no operational dependency — nothing requires them to be sequenced any particular way relative to each other, only relative to GLOBO. Creation order is used for all of them uniformly (not just GLOBO) simply because it's a natural, already-available, deterministic ordering to fall back on, not because the others need it specifically.

### 3.1 `vl network start [--env <env>]`

No `--as` — every underlying call is either local (`vl org start`, `vl node start`) or authenticated as each node's own service account (`vl-org-phase2-spec.md` §8, `vl-node-spec.md` §4). For each org in §3.0's order (GLOBO first): call `vl org start <org>`. GLOBO's own nodes start in ascending `org_node` order within it (`vl-org-phase2-spec.md` §8's own ordering, not restated here), and no stagger between orgs or between nodes within one — matching the script's startup loop, which has none either.

### 3.2 `vl network stop [--env <env>]`

No `--as` — see §4. For each org in §3.0's order, **reversed** (GLOBO last): call `vl org stop <org>` (`vl-org-phase2-spec.md` §8) — its own per-node ordering (descending `org_node`), CP-poll-with-timeout, and GLOBO-conditional pause (triggering correctly here specifically because GLOBO is, by construction, the last org in this sequence) all apply exactly as already specced there, with nothing restated or reimplemented in this document.

### 3.3 `vl network reset [--as <label>] [--yes] [--env <env>]`

**Requires `--as`, unlike `start`/`stop`** — reset needs an upfront, whole-environment guarantee that every node is actually down before touching anything, which (per the same reasoning as `status`, §3.4) needs cross-org visibility no single node's own credentials can provide.

1. Call the same bulk endpoint `status` uses (`GET /nodes`, no `orgNode` param, `cp-node-identity-spec.md` §2.5), authenticated as `--as <label>`, which must hold `PLATFORM_ADMIN`. If anything is not `DOWN`, **abort immediately** — print exactly which nodes, and delete nothing at all, not even for nodes that are down. This runs before the confirmation prompt below — no point asking "are you sure" if the answer has to be "no" regardless.
2. **Confirm**, unless `--yes` was passed: prompt `Reset ALL nodes in environment '<env>'? This permanently deletes local blockchain state for every node. Are you sure (y/N)?` (same semantics as `vl node reset`'s prompt, `vl-node-spec.md` §5.4 step 1, including the no-TTY-requires-`--yes` behavior) — no "will be stopped first" wording here, unlike earlier drafts of this prompt, since step 1 already guarantees everything's down before this point is ever reached.
3. For each org in §3.0's order, reversed (GLOBO last, for consistent, predictable output ordering — nothing here has GLOBO's actual timing-dependent significance anymore, since nothing is being stopped): call `vl org reset <org> --yes`.

**Unlike `start`/`stop`, this is not simply "what `vl org reset` already does, called once per org"** — `vl org reset` itself is unchanged (`vl-org-phase2-spec.md` §8 — still its own interleaved per-node stop/poll/reset, no upfront bulk check of its own) and running it standalone doesn't get step 1's all-or-nothing guarantee. That asymmetry is deliberate, not an oversight, for reasons beyond just "already tested and working": a network typically spans many more nodes than any one org (hundreds across a whole environment vs. a handful per org), and a network reset is typically paired with the heavier CP-side data deletion (studies, blocks — `reset-demo-environment-spec.md` §2) that an org-level reset never involves — scale and consequence both point toward the stronger, more deliberate guarantee mattering specifically at this layer, not at the org level where auto-stop's convenience still outweighs it. Revisiting `org reset` to match this same guarantee later is a reasonable thing to do, just not a priority now — not something this asymmetry is expected to force.

### 3.4 `vl network status --as <label> [--env <env>]`

The one `vl network` verb that **does** take `--as` (see §4 for why the other three don't) — a read-only, cross-org view of every node's live control-plane state in the environment, for a human checking in or a script (e.g. a reset-orchestrating wrapper outside this document's scope) verifying a precondition before doing something destructive. Resolves the calling identity per the standard precedence (`--as <label>` / `VL_IDENTITY` / environment default, `vl-org-spec.md` §5) and authenticates as it; the CP itself enforces that this identity holds `PLATFORM_ADMIN`, returning `403` otherwise (`cp-node-identity-spec.md` §2.5) — `vl` doesn't pre-check the role locally, it just surfaces whatever the CP says.

Calls the new bulk endpoint (`GET /nodes`, no `orgNode` param, `cp-node-identity-spec.md` §2.5). Since one CP instance backs exactly one environment (`vl-env-spec.md` §3, `cp_base_url`), the response is already this environment's complete node set with no further filtering needed — no explicit `veritaslock` exclusion required here the way §3.0 needs one, since a bulk *node* listing naturally has nothing to say about an org that, by design, never has any (§3.0). Prints one row per node returned — `org` (resolved from `org_id` via the local `organization` cache, `vl-org-spec.md` §3), `org_node`, `state`, `host` — with no interpretation of what those states *should* be. This command's only job is successfully retrieving and displaying them; **it exits `0` as long as it did that**, regardless of what states it found — an environment full of `UP` nodes is exactly as much a "success" here as one full of `DOWN` nodes, since checking that everything's actually up is just as legitimate a use of this command as checking that everything's down. It only exits non-zero on a genuine failure to retrieve the data at all (the CP unreachable, `403` from a caller lacking `PLATFORM_ADMIN`, etc.) — never based on the *content* of a successful response. An earlier draft had this command exit non-zero whenever anything wasn't `DOWN`, baking in one specific caller's use case (a pre-reset precondition check) into what should be a neutral, general-purpose status view; `vl network reset` (§3.3) doesn't call this command or depend on that behavior — it calls the same underlying bulk endpoint directly and does its own interpretation, so removing this command's built-in interpretation doesn't affect it.

---

## 4. Authentication

Unlike every other post-Phase-3 command group in this project, two of `vl network`'s four verbs take no `--as`. Nothing in `start`/`stop` requires an operator identity: both are purely orchestration over `vl org start`/`stop` (`vl-org-phase2-spec.md` §8), which itself either acts locally or authenticates as each node's own service account — never as whoever is running the network-level command. This mirrors why `vl node stop` and `vl org stop` themselves have no `--as` (`vl-node-spec.md` §5.3, `vl-org-phase2-spec.md` §8): an operator identity is only needed where a command asks the IdP to do something on the operator's behalf (as `vl node create` does, or as `status`/`reset` do below), not for a node checking in on itself.

`status` (§3.4) and `reset` (§3.3) are the exception, and for the same reason: both need to see across every org at once — `status` to report it, `reset` to guarantee it before deleting anything — which is precisely the kind of thing that requires an operator identity rather than a node's own self-scoped credentials. `reset`'s per-org, per-node work after that upfront check still goes through `vl org reset`/`vl node reset`'s own unauthenticated-at-that-layer mechanics, same as `start`/`stop` — only the initial bulk check needs `--as`.

---

## 5. Open Items

1. **GLOBO-specific ordering** (GLOBO-last/descending on shutdown per the script's `SHUTDOWN_NODE_DIRS` construction, §3.2; GLOBO-first/ascending on startup per its `NODE_DIRS` construction, §3.1) is carried over as-is without re-examining whether that's still the right rule now that orchestration lives in `vl` rather than a hand-maintained script. Worth a deliberate decision later rather than silently perpetuating it — same open item as `vl-node-spec.md` §8 item 2, tracked once there and referenced here rather than duplicated.
2. **No `vl network list`/`show`** — since a network has no backing row, there's nothing to list/show beyond what `vl org list` and `vl node` commands already surface per org. Flagging in case a future convenience view (e.g. a single "everything in this environment" status table spanning orgs and nodes) is wanted later; not part of this phase.
3. **`vl org reset` could eventually get the same bulk-check-and-abort guarantee `vl network reset` has** (§3.3), rather than its current per-node interleaved stop/poll/reset with no upfront whole-org check. Explicitly not a priority — the two commands' current asymmetry is a deliberate scale/consequence trade-off (§3.3's own note), not an oversight — but tracked here rather than left to be rediscovered as a surprise later.
