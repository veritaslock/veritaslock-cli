# Study lifecycle work tracker

Kept by hand from the design conversation. It is only as current as the last report in that conversation. Update it when something changes.

## Plan for tomorrow

0. The branch is complete: 13 commits, `4afa67b` through `9cb30fd`. Nothing more to add before merging.
1. Done: `studyupdated-at-mint` is merged into `features/python-cli` in the node repo (per you). Confirm the full suite shows only the 7 known failures after the merge.
2. Update `vl study delete` (`study.py:330`): drop `--force` and the hard `DELETE`; keep the `PATCH {deleted: true}`, `--yes`, and the local "already deleted" check, which prints a message and exits 0 (no error, no prompt, no request).
3. Push the four control-plane commits and the merged node branch.
4. Then test: deploy every node with the new binary together, reset dev data, run the lifecycle checklist and the `vl node` live checks below.
5. After testing: item 12 (needs your yes), then item 16.

## Control plane (veritaslock-services)

| Item | State | Next |
|---|---|---|
| PATCH validation: terminal and overdue-study rules (`cp-study-lifecycle-spec.md` §4.1, §4.8) | Committed `6b4d752`, not pushed | Push all four CP commits together (this, the guard, and the two below) |
| Late-report guard in `StudyUpdateHandler` (§6 item 11) | Committed (per you), not pushed | Push |
| Remove hard delete: `DELETE /v1/studies/{eventId}`, `force`, its filter rule and tests (§2) | Done: `c61ea45`, not pushed. A `DELETE` now returns 405 with `Allow: GET, PATCH`; the 409 tests pass; no leftover references except `vl` | Push |
| `GET /v1/nodes` scoped by caller, plus `scope=all` (`cp-node-identity-spec.md` §2.5) | Done (implemented earlier, per you) | — |
| Optional: the overdue-study rejection now reports field `studyState` | Done: `49af465`, not pushed | Push |
| Network reset endpoint `POST /v1/network/reset` (`cp-network-reset-spec.md`) | Built `d0b8993`; unknown query parameters rejected (`1356627`); not pushed; re-verified after `1356627`: 22 controller tests, 5 real-server filter tests and all 8 MariaDB tests pass; verified live (`vl network reset` then `vl network start` created the genesis block) | Add `export NETWORK_RESET_SUPPORTED=true` to `control_plane_service_setup.sh` for dev; qa has no config in the repos; push |
| Reject a study whose window has already ended (`cp-study-lifecycle-spec.md` §4.9): `400` against `endingTime` on create and on resubmission | Built `01bb904`, not pushed; 11 new tests; only the 2 known failures | Push |
| `rejectedReason` can now be long (`cp-study-lifecycle-spec.md` §6 item 15) | Done: truncates to the column's 65,535 bytes (`ee9d258`, not pushed) | Push |

Done earlier: `GET /v1/nodes` scoping and `scope=all`, the STANDBY endpoint, the `up_since` fix, the CURATOR/PUBLISHER refill on heartbeat timeout, the voter-set check.

## Node (veritaslock-node)

| Item | State | Next |
|---|---|---|
| Sweeper scope, `ENABLED` evaluation, handler lifetime fix, ingestion state check | Committed (7 commits) | Confirm pushed |
| Activation on the first data event, `processStudyEvents` seam, open-ended-study fix, rejected events no longer use up IDs | Committed (in those 7) | — |
| Cache hardening: `shared_ptr` return, one write lock, lookup outside the mutex, negative cache | Committed `250f01b`, `913d1b5`, `a0bf254`; not pushed at last report | Review, push |
| `StudyUpdated` applied at mint, branch `studyupdated-at-mint` | Merged into `features/python-cli` (per you), 13 commits `4afa67b` through `9cb30fd`; reviewed; push status unknown | Push; then test |
| `ENABLED` that resolves to the node's existing state still publishes (`e2bbfa9`) | Done | — |
| `NextTicker::getNextTick` and `BaseEventProducer::nowIso8601` used non-reentrant `gmtime`; node item 20 | Done (`8ef29b7`, `9cb30fd`); the audit found no others | — |
| Items 13, 16 and 21, one piece of work: an existing row at mint is skipped with an ERROR log and nothing else changes (item 13, corrected); `addBlock` is retry-safe and mint handlers never throw on data-dependent input (item 16); add a study to the cache at mint as insert-if-absent, keep the consumer's add, then stop reading studies from the database after the startup load (item 21) | Agreed; not started | One combined prompt after testing, with a report-only step first |
| Item 22: the node rejects a submission whose `endingTime` is not after `createdAt` (or not after `startingTime`), from event data only; also fixes a string comparison of times and now sends the specific reasons in `REJECTED` | Merged into `features/python-cli` (per you), with item 23's sweeper fix | Push |
| Item 23: the sweeper's query compared `ends_at` as text, so a study ending on a sweep boundary was finalized one sweep (10 s) late | Done and merged (`fb0d07c`) | Push |
| Item 24: nodes get `403` fetching a signer's public key from the identity provider (commit `0fa2357`, 2026-09-25), so every data event is rejected as "Invalid signature" and peer authentication would fail | Node logging fix built (`c43efc1` on `svcacct-lookup-visibility`; 256 tests, only the 7 known failures; ASan and TSan clean; not pushed). The identity provider endpoint (`/public`, returning `id`, `orgId`, `publicKey`) and the node's switch to it are not built | Review and merge the node branch; run the identity provider prompt (without `role` in the response); then a small node change to call the new endpoint. **Blocks the baseline:** lifecycle steps 2, 3 and 9 need events |
| Item 19: every protocol event exempt from the org-viability exclusion (`1f86bf1`) | Done | — |
| Item 16: make `addBlock` retry-safe, handle an existing study row at mint without throwing and evict the cache entry | Not started | Prompt after the merge |
| Item 12: release the event IDs and participation keys of events a block leaves out | Done (`d19056f`): released when the event is erased from the pool at mint; all five checks satisfied | — |
| Items 6 (leader-publish gap) and 17 (derive `FINALIZED` at mint) | Deferred | — |

## vl (yours)

- **Broken until done:** `vl study delete` (`study.py:330`) still sends the removed `DELETE`, for `--force` and for an already-deleted study, and now gets a 405. Fix: remove `--force` and the hard `DELETE`; keep `PATCH {deleted: true}`, `--yes`, and the local "already deleted" check, which prints a message and exits 0 (no error, no prompt, no request).
- `vl study update`: print that nodes apply a change after the next block, about 10 seconds.
- `vl network status` and `reset` are built. **`vl node list` / `show` with the control-plane overlay and computed states are now built** (`vl-node-spec.md` §4c, §5.5, §5.6): five commits on `feat/vl-env` (`27ef0db`, `12fbf74`, `54e2404`, `3ea332d`, `0dc7fef`), not pushed; 351 tests pass, mypy clean, 117 new tests, three deliberate breakages each caught. The same work fixed a real safety bug: `vl node stop` and `start` used a bare `kill -0`, so after a pid reuse `stop` could signal an unrelated process; both now use the "our process" check. Review closely: `vl.lib.proc` and `vl.lib.node_state.displayed_state`.
- Small fix for the authorization work: `NodeNotFoundError` has an empty message, so `node start`, `stop` and `reset` on a missing node print a blank `Error:`. Only `show` has a proper message now.
- `vl study` tests: **built** on branch `vl-study-tests` (`c40f201` delete 20 tests, `aaa302e` update 28, `15edce3` add 15, `b2bd477` show 9 and list 12, `ffb75b5` fix with 3 tests, plus 8 no-identity tests; 95 in all; the suite is at 558; mypy clean; not pushed). It found one real bug, a lookup returning two studies with the same `studyId` took the first (fixed in `ffb75b5`, the only non-test change). **Decisions:** (1) unknown options print help and exit **0** for every `vl` command (`HelpOnErrorGroup`), so a stale `--force` in a script silently does nothing; recommend exit 2, report-first prompt in the conversation; (2) `add` should refuse a file that isn't a JSON object, and `delete` may hint to `--disable` first (small prompt in the conversation). Also review `ffb75b5`, then merge.
- Node startup (`verilock-node-startup-spec.md` §2.2 step 6, `/standby` before catching up): **built** in `fc08e6f` (2026-09-28), checked by the node agent. One gap: recovery after `UP` (`Heartbeat.cpp:80`) still publishes `NodeUp` over Kafka and not `PATCH /up`; the follow-up prompt is in the conversation. No tests exist for any of it (they need `AppCntx` and live HTTP). The recovery fix is built on branch `heartbeat-up-over-rest` (the build was repaired in `a3dcac4`; the full suite shows only the 7 known failures; ASan and TSan are clean); not pushed or merged. In `Heartbeat.cpp`'s `409` handler the recovery calls `nodeIdentity().nodeUp()` directly (verified from the pasted code). **Verified from the code:** the whole loop body is in `try { … } catch (const std::exception&)` that logs and carries on, and the loop sleeps 5 s between heartbeats, so a failed `PATCH` is retried by the next heartbeat's `409` and recovery should take about 5 to 6 s. Only `std::exception` is caught; adding `catch (...)` is optional. Still to run: the live tests (steps 1 to 4, and step 6 with the control plane down). The choice between `STANDBY` and `UP` is unit-tested; the actual recovery needs the live `kill -STOP` / `kill -CONT` check (live check 2 below) before merging. The agent found no other publisher of `NodeUp`/`NodeStandBy`; `NodeDown` is still published over Kafka at shutdown, so a late `NodeDown` can arrive after a fast restart's REST `/up` (the recovery path heals it); a REST `/down` is worth considering later.
- `vl network reset` calls the new endpoint first (dry run, confirm with counts, real call, then per-org `vl org reset --yes`); any control-plane failure aborts with every node untouched (`vl-network-spec.md` §3.3). Done, per you: a reset followed by a start worked end to end and created the genesis block.
- `vl node reset` and `vl org reset` take `--include-snapshots` (default: keep `<node_root>/snapshots`, a per-node peer of `data`); with it they delete and recreate that directory, and `vl org reset` passes it through to `vl node reset`. `vl network reset` always passes it (`vl-node-spec.md` §5.4, `vl-org-phase2-spec.md`, `vl-network-spec.md` §3.3). Not started.
- Authorization of `vl` commands by role (`vl-authorization-spec.md`): **built** on `feat/vl-authorization` (`07ec9d1`, `dbe3374`, `cd486a2`, `7230c06`, `2274788`), not pushed or merged. 463 tests pass, mypy clean, 112 new tests; removing the node checks fails 20 tests and the network checks 10. Review closely: `require_operator` and the three rules in `_shared.py`, and that the check is the first thing each command does. **Note:** scripts (including `reset-demo-environment`) now need a default identity with the right role, or `--as`; set yours with `vl usr-acct use <label>`.

## Specs

- Copy every file from the outputs folder into `docs/` in each repo. Close the editor tabs first; stale open copies keep overwriting newer ones.
- Changed in this work: `cp-study-lifecycle-spec.md`, `node-study-lifecycle-spec.md`, `vl-study-spec.md`, `reset-demo-environment-spec.md`, `cp-node-identity-spec.md`, `vl-node-spec.md`, `vl-network-spec.md`, `verilock-node-startup-spec.md`, `consensus-voter-set-spec.md`, `cp-network-reset-spec.md` (new), `vl-authorization-spec.md` (new), `idp-service-account-public-lookup-spec.md` (new), `protocol-event-merkle-root-signed-votes-spec.md`, `block-header-builder-minter-spec.md`.
- The updated specs have been copied into `docs/` in the repos (done, per you). Keep replacing wholesale when a new version is handed over, with the editor tabs closed.
- After the node branch merges: rewrite `node-study-lifecycle-spec.md` §3, which still describes the old direct handler.

## Decisions waiting on you


## After the node branch merges

1. Deploy every node with the new binary together.
2. Reset dev data (the existing chain files hold duplicated blocks).
3. Run the lifecycle checklist.
4. Then items 16 and 12.

## Live checks for the `vl node` states

Checks 1 to 5 can now run (`vl node list` and `show` are built, on `feat/vl-env`, not yet pushed). Check 6 (`vl network`) works too.

1. Reboot or `kill -9` a node's process: `vl node list` shows `STOPPED` and clears the pid, with no control-plane call.
2. `kill -STOP` a running node (freezes it, heartbeats stop). After 30 to 35 seconds the control plane marks it `DOWN` while the process is alive: `NO_HEARTBEAT`. `kill -CONT` afterwards. Afterwards `kill -CONT` it: the control plane should show it `DOWN` and then back to `UP` with the same pid (via REST once the recovery gap is fixed).
3. Start `verilock` by hand from a node's directory, outside `vl`, with `vl` showing it `STOPPED`: `UNMANAGED`.
4. Change a stopped node's `host` in the control plane's `node` row to another address: `REMOTE`.
5. Run `vl node list` with the control plane down: a warning and local rows only. Run it with credentials that can't see the org: an error and nothing listed.
6. Run `vl network status` and `vl network reset` as an org admin: both get `403`; as a platform admin they succeed.

## Lifecycle checklist (after deploy and reset)

1. Submit a study with a future start: `ACCEPTED` on the control plane and the node, and it stays `ACCEPTED` with no events.
2. After the start, send an event. Once its block mints, the study is `ACTIVE` on both sides.
3. Disable it (about a block to reach ingestion). An event sent now is rejected; re-enable, and resending the same event ID is accepted.
4. Enable a study that never had an event: it goes to `ACCEPTED`.
5. Edit `endingTime`, the description and the name on an active study: the node shows the new values (times within a second or two, state changes after the next block).
6. Let a short-window study expire: the control plane shows `FINALIZED`, an edit returns 400, and a soft-delete returns 400.
7. Disable a study and let its end pass: it stays `DISABLED` and can be soft-deleted. Extend `endingTime` and enable in one `PATCH` to resume one instead.
8. Soft-delete a `REJECTED` study directly; resubmit another `REJECTED` one with `add` and with `update`. A `POST` with a soft-deleted study's ID returns 409. **Not testable live:** the control plane enforces the study id pattern (an id longer than 15 characters, and one containing a dot, were both refused with `400`), so no study the node would reject can be created through the API. Covered by unit tests instead (the control plane's `PATCH` and `POST` tests). The `vl` already-deleted behavior was **verified live**: `vl study delete fulcrum FUL-SOX-FY26` on a deleted study printed `Study fulcrum/FUL-SOX-FY26 is already deleted` with no prompt and no `--yes`; the exit status is `0` (checked); still to check that a test for it exists in the CLI repo (`cd` into it and run `grep -rn "already deleted" tests/`, which searches the test source, not a log). The exact pattern the control plane enforces is in `CreateStudyRequest`; confirm it matches the node's (1 to 15 characters, letters, digits, `_`, `-`).
9. Create an open-ended study and send it an event.
10. Restart a node partway through and check the states reload.

## Node build notes

- Build in `cmake-build-debug` with **CLion's bundled CMake only** (the one named in `CMakeCache.txt`), never `/usr/bin/cmake`. A different CMake makes FetchContent re-clone dependencies.
- Don't run a configure unless a `CMakeLists.txt` changed. To build: `<CLion cmake> --build cmake-build-debug --target <target>`.
- Editing a `CMakeLists.txt` makes CLion reload on its own. Never run a second configure while one is in progress (`pgrep -af 'cmake|git (clone|index-pack)'`): two FetchContent clones then race in `_deps` and corrupt `log4cplus-src`.
- Recovery: with nothing running, delete `_deps/log4cplus-src` and `_deps/log4cplus-subbuild`, then CLion > Tools > CMake > Reset Cache and Reload Project.

## Parked items (one at a time, in this order)

1. **Apply the replacement text to `docs/cli-reference.md` lines 6 to 9** (usage errors now exit 2) and commit it as a `docs:` commit on `vl-study-tests`. Now.
2. Review and merge `vl-study-tests` (commits `c40f201`, `aaa302e`, `15edce3`, `b2bd477`, `ffb75b5` (the bug fix in `study.py`, read this one), `e7a2157`, `2cc54ca`, plus the docs commits). 95 + 13 new tests; the suite is at 583; mypy clean. Usage errors now exit 2 (`2cc54ca`); the script search found only `cache-disk-users.sh` calling `vl`, and the change makes it more accurate.
3. Replace the stale `verilock-node-startup-spec.md` in the node repo (it lacks the recovery-fix status), if not already done.
4. Send me the commit IDs and branches for the identity provider endpoint and the node's switch to it (item 24).
5. Cut the baseline: node branches, specs in the node and services repos, `--no-ff` merge to `main`, tag, then branch `mint-hardening`.
6. Later: the combined node work on items 13, 16 and 21 (starts on `mint-hardening`), and the multi-host agent (waiting on your answers to my five questions).
7. Later, low priority: convert `send_events.sh` and `start_test_harness.sh` to use `vl` (`vl study add` replaces the ad hoc study call; event sending needs a design, since it depends on the client keys `vl` manages). The agent that searched the scripts did not search Python or other non-shell callers of `vl`.
