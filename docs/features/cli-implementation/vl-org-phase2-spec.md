# veritaslock-cli (`vl`) — Phase 2 update: `vl org start` / `stop` / `reset`

**Status:** Draft — new addition, not yet merged into `vl-org-spec.md`.
**Not a change to anything currently in `vl-org-spec.md`** — these three verbs don't exist there yet in any form. This document is a complete, standalone spec for them, meant to be merged into `vl-org-spec.md` as new `§4.9`-equivalent content once implemented, so that document ends up with a complete, current picture. Until then, `start`/`stop`/`reset` live here only.
**Depends on:** `vl-org-spec.md` (Phase 2, for the `organization` cache table and the org this document's commands are scoped to), `vl-node-spec.md` (Phase 7, for the `node` table and the `vl node create`/`start`/`stop`/`reset` verbs this document orchestrates), `vl-network-spec.md` (Phase 8, the same orchestration pattern one level up, for comparison).

---

## `vl org start` / `stop` / `reset`

Orchestration only — no new columns or tables on `organization`. Each verb here calls the same-named `vl node` verb (`vl-node-spec.md` §5.2–§5.4) once per node belonging to this org, exactly as `vl network start`/`stop`/`reset` do for every org in an environment (`vl-network-spec.md` §3). This is the org-scoped instance of that orchestration; see `vl-node-spec.md` for the per-node mechanics (spawn/kill/CP-status-check details) — they aren't repeated here.

**None of the three take `--as`** — unlike every other command in `vl org`. Nothing here needs an operator identity: `start` is local orchestration over `vl node start` (itself local), and `stop`/`reset`'s one class of REST call — the control-plane status check for a given node — authenticates as that node's own service account (resolved via its `svc_acct_id`, `vl-node-spec.md` §4), not as whoever ran `vl org stop`/`reset`. Same reasoning as `vl-network-spec.md` §4. All three do accept `--help` explicitly, same as every `vl node` command (`vl-node-spec.md` §5).

**Remote node execution (SSH):** same note as `vl-network-spec.md` §1 — this orchestration is entirely "call the same-named `vl node` verb per node," so it inherits whatever remote-host mechanism `vl node start`/`stop` eventually gain (`vl-node-spec.md` §7, planned fast-follow) with no changes needed here.

- **`vl org start <name> [--env <env>]`** — for each of the org's nodes, call `vl node start`. No ordering, no stagger.
- **`vl org stop <name> [--env <env>]`** — reverse-of-startup order within the org (descending `node<N>`), each node stopped via `vl node stop`, skipping any node `vl` didn't start (no `pid` on record — e.g. one running under a debugger). After each node's process exits, polls the control plane (`GET /nodes?host=&port=`, `vl-node-spec.md` §5.4's endpoint, authenticated as that node's own service account) until it reports `DOWN` before pausing (`1`s between nodes, `3`s before the org's last node) and moving to the next. Same rationale as the network-level version: the pause exists so surviving nodes have consumed the departing node's `NodeDown` event before the next one goes down, not an arbitrary delay.
- **`vl org reset <name> [--yes] [--env <env>]`** — confirms once for the org, not once per node (`Reset ALL nodes in org '<name>'? ...`, same shape and semantics as `vl network reset`'s batch prompt, `vl-network-spec.md` §3.3), then applies each node's reset steps directly with the per-node prompt suppressed. No ordering, no stagger — resets are independent of each other.

`vl network start`/`stop`/`reset` (`vl-node-spec.md` §7) are this same orchestration one level up — iterating every org in the environment (`vl-network-spec.md` §2 — a network has no table of its own) and, for `stop`, treating the *entire environment's* node set as one ordered sequence (not per-org sequences run independently), preserving `start_test_harness.sh`'s original global shutdown order.

---

## Open Items

1. **GLOBO-specific shutdown ordering** (carried over from `vl-node-spec.md` §6 item 2, and originally from `start_test_harness.sh`): org sorted last, descending `nodeN`. Inherited as-is by `vl org stop` for whichever org it applies to, without re-examining whether that rule still makes sense now that orchestration lives in `vl`. Not resolved here — to be added to `vl-org-spec.md`'s own Open Items when this document is merged in.
