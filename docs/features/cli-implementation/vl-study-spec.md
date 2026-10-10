# veritaslock-cli (`vl`) — `vl study`

**Status:** Draft for implementation. Previously paused pending the control-plane redesign (`cp-study-lifecycle-spec.md`) — that work is done, this document now covers the full command surface.
**Depends on:** `cp-study-lifecycle-spec.md` (the full REST contract this implements — endpoints, request/response shapes, the `studyId` verification requirement, resubmission behavior, authorization).
**Builds on:** `start_test_harness.sh`'s existing `mint_study_token`/study-fixture-publishing logic (`reset-demo-environment-spec.md` §2 step 6), which already calls `POST /v1/studies` today — `vl study add` replaces that ad hoc call, not new server-side behavior. `vl team add <org> <name>`'s positional-org-then-argument shape (`vl-team-spec.md` §5.1), matched throughout.

---

## 1. Scope

Five verbs: `add`, `show`, `list`, `update`, `delete`. **No local cache table** — unlike `organization`/`team`, `study` is pure REST pass-through, deliberately, to avoid `vl` ever holding a stale, out-of-sync copy of CP-owned business data.

**`eventId` is never something `vl` caches or reuses across calls.** `studyId` is the stable identity a person addresses a study by; `eventId` is the database's own internal key, and it can change out from under a caller — most notably on resubmission of a `REJECTED` study (`cp-study-lifecycle-spec.md` §4.7), which deletes the old row and creates a new one with a fresh `eventId`. Every command below that needs `eventId` (`show`, `update`, `delete`) resolves it fresh, immediately before use, via `list`'s `?studyId=` filter — never from a value obtained in an earlier call. **That internal resolve always includes deleted studies** (`includeDeleted=true`, regardless of `list`'s own user-facing default), independent of `list`'s own `--include-deleted` flag — a targeted lookup by a specific, already-known `studyId` isn't the general browse that flag exists to keep uncluttered by default. Without this, `update` and `delete` on a soft-deleted study would report "not found" for a study that does exist instead of the CP's real answer, and `show` could not confirm that a soft-delete took effect.

## 2. Commands

### 2.1 `vl study add <org> <file> [--as <label>]`

`POST /v1/studies`. `vl` reads `<file>` and sends it essentially as-is — no local parsing or validation beyond what's needed to send it (matching the "don't pre-validate what the server will validate" principle used throughout this project). The associated team is part of the study payload's own JSON schema, not a separate `--team` flag.

**`<org>` is, strictly speaking, not needed here** — the payload's own `orgName` field is what the CP actually acts on (`cp-study-lifecycle-spec.md` §4.7's resolved-`orgId` logic doesn't look at anything else). `vl` checks `<org>` against the file's `orgName` before sending, and refuses on a mismatch — a deliberate, narrow exception to the "don't pre-validate what the server will validate" principle above, specifically to catch the case of running `vl study add` with the right file but the wrong `<org>` on the command line (or vice versa), which the server has no way to flag as a mistake since both values would be independently valid on their own. `show`/`update`/`delete` (§2.2) have the same kind of check, for the same reason, against a server lookup rather than a file.

**Transparently handles resubmission of a `REJECTED` study too** — same command, no special flag. If `<file>`'s `studyId` matches an existing `REJECTED` study, the CP treats it as a fix-and-resubmit rather than a conflict (`cp-study-lifecycle-spec.md` §4.7); `vl` doesn't need to know or care which case it is, it just sends the request and reports whatever comes back. **Exception: if that `REJECTED` study has already been soft-deleted, the CP refuses with `409`** instead of resubmitting (§4.7's `deleted != true` requirement) — `vl` needs no code change for this, since it already just reports whatever the server says, but it does mean "transparently handles resubmission" isn't quite universal: a soft-deleted `REJECTED` study's `studyId` stays taken, so the submission needs a different `studyId`.

`--as` required — see §3.

### 2.2 `vl study show <org> <study-id> [--as <label>]`

Implemented as `list` (§2.3) filtered to `?studyId=<study-id>`, expecting exactly zero or one result — **not** a resolve-to-`eventId`-then-fetch-by-`eventId` two-step. Since `studyId` is globally unique, the filtered `list` call already *is* the single-study lookup; there's no separate by-`eventId` endpoint this needs to front, and no staleness window to worry about the way `update`/`delete` have (a read has nothing to act on incorrectly). **Always includes deleted studies** (§1) — unlike `list`'s own `--include-deleted`-gated default, since `show` is inherently a targeted lookup by a `studyId` the caller already knows, not a general browse. This is what makes it possible to confirm that a soft-delete took effect, rather than getting a misleading "not found" for a study that genuinely still exists.

**`<org>` is not sent to the server anywhere in this resolve** — `studyId` is globally unique, and `list`'s own request carries no org parameter at all; scoping comes entirely from the authenticated identity (`--as`), not from anything the user types. So `<org>` is verified **locally**, after the resolve: the found study's actual org is compared against `<org>`, and a mismatch is a clear, specific error, distinct from "not found" — the study exists, just not where the caller believed. This exists for the same reason as the `studyId`/`eventId` verification on `update`/`delete` (`cp-study-lifecycle-spec.md` §2) — `studyId` is short, user-chosen, and not reliably tied to org in anyone's memory, so someone managing several orgs could easily get the `studyId` right while being wrong about which org they think it belongs to, and silently act on the real org instead of the intended one. `update` and `delete` resolve identically (§2.4, §2.5) and get the same check, not repeated in each of their own descriptions — same idea as `add`'s check against its payload file (§2.1), just against a server lookup instead, since there's no file here to compare against.

`--as` required — see §3.

### 2.3 `vl study list [--study-id <id>] [--study-name <name>] [--include-deleted] [--as <label>]`

`GET /v1/studies`, scoped by the caller's own JWT (every org's studies for a `veritaslock`-member caller, else just their own org's — `cp-study-lifecycle-spec.md` §1). `--study-id`/`--study-name` map to the server's `studyId`/`studyName` query params and compose with that scope rather than bypass it. `--study-name` can return more than one row for a `veritaslock`-scoped caller, since it's only org-unique, not globally unique (`cp-study-lifecycle-spec.md` §3). `--include-deleted` maps to the server's `includeDeleted` param, off by default.

**No `--org` filter** — the server-side endpoint doesn't have one (`cp-study-lifecycle-spec.md` §6 item 5, still open); a `veritaslock`-scoped caller currently has no way to narrow to one org through this endpoint at all, server- or client-side.

`--as` required — see §3.

### 2.4 `vl study update <org> <study-id> [--study-name <name>] [--description <text>] [--starting-time <iso8601>] [--ending-time <iso8601>] [--allow-multiple-events-per-client/--no-allow-multiple-events-per-client] [--disable] [--enable] [--as <label>]`

Resolves `<study-id>` to its current `eventId` via `list`'s `?studyId=` filter, with the same local `<org>` verification `show` does (§2.2), then `PATCH /v1/studies/{eventId}?studyId=<study-id>` — the `studyId` query param is required by the CP as a verification check against the just-resolved `eventId`, not merely descriptive (`cp-study-lifecycle-spec.md` §4.1). Only the flags actually supplied are sent — an ordinary partial update, same convention as `vl env update`.

**`--allow-multiple-events-per-client`/`--no-allow-multiple-events-per-client` is a paired flag, not `--allow-multiple-events-per-client <bool>` taking a value** — a single-name boolean flag can only ever be *present* or *absent*, which means "present" and "true" collapse into the same case and there's no way to actually send `false`; a study could be turned on but never back off. The pair, backed by a three-state `bool | None` with a default of `None`, gives `vl` the three outcomes a partial update needs: `--allow-...` sends `true`, `--no-allow-...` sends `false`, and supplying neither leaves it at its `None` default, which means "not given" and sends nothing at all, same as every other flag on this command. An earlier draft of this document specified the single-value form, which doesn't actually work.

`--study-name` renames the study — non-blank, at most 255 characters (`400` otherwise, matching `add`'s own validation for the field), and rejected with `409` if another study in the same org already holds that name (`cp-study-lifecycle-spec.md` §4.1) — including a `REJECTED` one, unlike `add`'s resubmission path, which clears a same-`studyId`-and-`REJECTED` collision out of the way rather than treating it as a conflict. A rename to the study's current name is a no-op, not an error. Works in any state the other field-update flags do; on a `REJECTED` study it rides along with resubmission (§4.7 of that document) the same way the other fields already do.

`--disable`/`--enable` set `state: DISABLED`/`state: ENABLED` respectively — mutually exclusive with **each other**, rejected locally (`vl`'s own `CliError`, in `validate_input`, before any request is sent) if both are supplied. `state` is a single field that can only ever hold one value, so the server never actually sees this case to reject it itself. They are **not** exclusive with the field-update flags — the server allows combining a state transition with ordinary field updates in the same call, so `vl` does too; an earlier draft of this document claimed a broader exclusivity that doesn't hold, citing a note that isn't actually in `cp-study-lifecycle-spec.md` §4.1. `--enable` triggers the async evaluation path (`cp-study-lifecycle-spec.md` §4.5) — the study may not yet show its resolved state (`ACTIVE` or `ACCEPTED`) immediately after this command returns; a subsequent `show` reflects the real outcome once the node's responded. On a study already past its `endingTime`, `--enable` alone is refused by the CP; to resume one that was on hold past its end, extend `endingTime` in the same call (`--ending-time <t> --enable`; the node evaluates `ENABLED` against the new `endingTime` carried in the same event, `node-study-lifecycle-spec.md` §3) or in an earlier update.

Soft-delete (`deleted: true`) is **not** available through this command — that's `delete`'s job (§2.5), kept separate since it's the one irreversible step in this list.

**Ordinary field updates against a `REJECTED` study are a second path to resubmission**, alongside `add` (§2.1) — the CP publishes `StudySubmitted` instead of an ordinary patch (`cp-study-lifecycle-spec.md` §4.1). Same as `add`: `vl` doesn't need to detect or special-case this, it just sends the request.

**A study in `FINALIZED` or `ARCHIVED` can't be updated at all** — the CP refuses with `400` (`cp-study-lifecycle-spec.md` §4.8), and `vl` reports it as it does any server refusal. Soft-deleted studies are refused the same way. Likewise, once an `ACCEPTED` or `ACTIVE` study's `endingTime` has passed, every update is refused, `--disable` included (`cp-study-lifecycle-spec.md` §4.8), so an operator who wants such a study gone has to disable and delete it before its end. `--enable` on an overdue `DISABLED` study is refused unless the same call also sets `--ending-time` to a future time (or clears it). `delete` isn't affected. A `REJECTED` study can still be edited, since changing its times is how it gets fixed, and so can a `DISABLED` one's ordinary fields.

`--as` required — see §3.

### 2.5 `vl study delete <org> <study-id> [--yes] [--as <label>]`

Resolves `<study-id>` to its current `eventId` via `list`'s `?studyId=` filter, with the same local `<org>` verification `show` does (§2.2), same as `update`. Then `PATCH /v1/studies/{eventId}?studyId=<study-id>` with `{deleted: true}`: soft-delete, and the only kind of delete there is. The row stays, hidden from `list` by default and still found by `show`; the study can no longer be modified or restored, and its `studyId` and name stay taken (`cp-study-lifecycle-spec.md` §2, §6 item 13). There is no hard delete and no `--force`: earlier drafts of this document and the CP had both, and they were removed. Clearing studies, or purging a row, is done with SQL (`reset-demo-environment-spec.md`).

Valid from `DISABLED` (the study must already have been explicitly disabled first, via `vl study update --disable`) **or directly from `REJECTED`**, with no `disable` step required: a `REJECTED` study was never accepting events in the first place, so there's nothing to disable (`cp-study-lifecycle-spec.md` §4.1). This command never disables anything itself, in either case.

**A study that's already soft-deleted is not an error.** `vl` already has the study's `deleted` value from the resolve step (§1), so it prints that `<org>/<study-id>` is already deleted and exits `0`, before the confirmation prompt: nothing is sent, and there is nothing to confirm. This matches `vl node start` and `vl node stop` on a node that's already in the target state (`vl-node-spec.md` §5.2, §5.3), and it keeps a cleanup loop running `vl study delete --yes` over a list from failing because one study was deleted earlier. Real failures still raise a `CliError`: the study isn't found, `<org>` doesn't match the study's org, or the server refuses the `PATCH`. (Without the local check, the `PATCH` would reach the CP and be refused there anyway, `400`, "the study is soft-deleted and can no longer be modified": correct, but worded for someone trying to *edit*.)

**Confirms before acting**, unless `--yes` is passed: `Delete <org>/<study-id>? It will be hidden from listings and can no longer be modified or restored. Are you sure (y/N)?` Only `y`/`Y` proceeds, matching every other destructive confirmation in this project (`vl node reset`'s prompt, `vl-node-spec.md` §5.4). Once `deleted = true` there is no reverse path anywhere in this lifecycle (`cp-study-lifecycle-spec.md` §4.1: no further `PATCH` is accepted), unlike `DISABLED`, which `--enable` can still undo.

**A study in `FINALIZED` or `ARCHIVED` can't be deleted**: the CP refuses with `400`, and `vl` reports it. This is deliberate (`cp-study-lifecycle-spec.md` §4.8), not a gap in `vl`: those studies reached their end, and the CP offers no delete for them. `REJECTED` studies differ: they never ran, so they delete directly (above).

`--as` required — see §3.

## 3. Authorization

Every verb requires `--as <label>` — an authenticated caller, resolved per the standard precedence (`vl-org-spec.md` §5). What role that identity needs to hold differs by verb (`cp-study-lifecycle-spec.md` §5), and `vl` doesn't pre-check any of it locally — it sends the request and surfaces whatever the server returns:

- **`add`/`update`/`delete`:** the target study's org's `ORG_ADMIN`, or (if it has a team) that team's `TEAM_ADMIN` — either is sufficient. `403` for anyone else, including `PLATFORM_ADMIN`.
- **`list`:** any authenticated caller, scoped by org membership (§2.3) — no specific role required.
- **`show`:** any authenticated caller, scoped by org membership the same way `list` is (`cp-study-lifecycle-spec.md` §5).

## 4. Open items

1. **`list`'s pagination UX** — `cp-study-lifecycle-spec.md` §1 specs keyset pagination server-side (`limit`/`cursor`). Deferred deliberately, not left undecided: `vl study list` transparently pages through everything server-side and prints one combined result, with no `--limit`/`--cursor` exposed in this first pass — study counts aren't expected to be large enough soon to need more, and this stays a backward-compatible addition later if that changes.
2. ~~`update`'s flag-based shape~~ — **Resolved:** flags, confirmed (§2.4 as drafted) — every field is a simple scalar with no nested structure a file would meaningfully help with, and the tradeoff is understood and accepted: flags tie the CLI more closely to `PatchStudyRequest`'s exact shape, so a future change to that request/event schema will need a corresponding CLI update, unlike a raw pass-through file.
3. **No `--org` filter on `list`** (§2.3) is a direct consequence of `cp-study-lifecycle-spec.md` §6 item 5 (no server-side org filter exists) — resolved automatically if that's ever added, not something to fix independently here.
4. ~~The `PLATFORM_ADMIN`-discovers-but-`ORG_ADMIN`/`TEAM_ADMIN`-required-to-act mismatch~~ — **Resolved:** the wrapper uses a `SYSTEM`-role service account instead, which bypasses per-study org/team authorization entirely (`node-study-lifecycle-spec.md` §8 item 3) — though the wrapper rework this was for is no longer planned, since studies in a terminal state can't be deleted through the API at all (`cp-study-lifecycle-spec.md` §4.8); the resolution stands for any future caller needing cross-org write access. Safety depends on that account's private key being genuinely restricted to the automation that needs it, not a property of the mechanism alone.
5. ~~The cross-org resubmission validation question~~ — **Resolved:** `cp-study-lifecycle-spec.md` §4.7 confirms this is properly validated (resolved `orgId` comparison on `add`, structurally excluded on `PATCH`). `add`'s behavior (§2.1) is as specified, no gap inherited.
6. ~~Disabling a study whose window may pass before it's deleted~~ — **Resolved:** a `DISABLED` study is no longer finalized by the clock, so it stays deletable until an operator acts (`cp-study-lifecycle-spec.md` §6 item 10). Nothing for `vl` to change.

## 5. Future: manual archive

A manual archive command is wanted alongside automatic archival of long-finalized studies, once the system runs in the cloud and the CP side exists (`cp-study-lifecycle-spec.md` §7). It is not specified here: no verb, flags, or confirmation wording are decided, and it depends on a CP endpoint that doesn't exist yet. Until then `ARCHIVED` is a state `vl` can display but cannot produce.
