# veritaslock-cli (`vl`) — Phase 2: `vl org`


> **Naming note:** the command surface was later restructured — `vl identity` folded into `vl usr-acct` / `vl svc-acct`, `vl user` → `vl usr-acct`, `vl service-account` → `vl svc-acct`, `vl org` moved to the `--as` auth model, and `vl org members …` split out into a top-level `vl org-members` command, peer to `vl org` (the same shape as the `vl team` / `vl team-member` split). Behaviour, schema, and rationale below are unchanged; see `vl-command-restructure.md` for the mapping.
**Status:** Draft for implementation
**Part of:** the larger `vl-cli-phase1-crud-spec.md` effort, broken out as its own standalone document. Depends only on `vl-env-spec.md` (Phase 1) — deliberately sequenced before the identity/user document (Phase 3), which depends on this one for its `org_membership` join table; see §5 for how write commands authenticate without needing Phase 3's identity store.
**Builds on:** the `environment` table and `vl env` commands from Phase 1; the live organization endpoints on the IdP (`getById`, `getByName`, `create`, `addMember`, `patch`, `patchByName`), plus three new endpoints specced separately for a server-side session in `idp-org-membership-endpoints-spec.md`: list all orgs, list an org's members, and remove an org member.
**Supersedes:** the `vl org` section of `vl-cli-phase1-crud-spec.md` (§8 in that document) — this is the authoritative version going forward.
**See also:** `vl-org-keys-spec.md` (Phase 5) — org event-encryption keys, a separate command group (`vl org keys ...`) discovered after this document was written; §4.8 below references it as the final bootstrap step.

---

## 1. Scope

This document covers:
- A standalone local `organization` cache table (per-environment, no local secrets — orgs have nothing to keep confidential).
- The `vl org` command group: `add`, `show`, `list`, `update`, `members add`, `members list`, `members remove`. No `delete` (destructive, cascading, deliberately out of scope). (The `members …` verbs later moved to their own top-level `vl org-members` command — see the naming note above.)

**Deferred to the `vl identity`/`vl user` document (Phase 3):** the `org_membership` join table (mirroring the IdP's own `organization` + `user_org_role` split), which will FK against both this document's `organization` table and that document's `identity` table. That document also retrofits `members add` (below) to write into it once it exists, so `members add`'s implementation should expect a small follow-up change when Phase 3 lands — see that document's §9.6.

**Deferred generally:** `vl org delete`, and upgrading write commands to accept `--as <label>` once a local identity store exists (§5) — the latter has since shipped; see the naming note above and `vl-command-restructure.md` §4.

---

## 2. Storage

Same `store.db` as Phase 1. This document adds one table.

## 3. Schema: `organization`

| Field | Type | Notes |
|---|---|---|
| environment_name | text | `REFERENCES environment(name) ON UPDATE CASCADE ON DELETE RESTRICT` |
| name | text | canonical lowercase name, matches the server's `organization.name` |
| server_org_id | text | the id IdP assigned |
| display_name | text | |
| active | integer | 0/1 |
| created_at | text | ISO8601 — mirrors the server's own `organization.created_at`, captured the same way `display_name`/`active` are on every upsert; not this row's own local creation time. Exists specifically so `vl network start`/`stop`/`reset` can determine org iteration order (`vl-network-spec.md` §3.0) from `store.db` alone, without a network round-trip just to ask the server what order its own orgs were created in. |
| synced_at | text | ISO8601 — updated every time this row is touched by a successful `vl org` call |

`PRIMARY KEY (environment_name, name)`. Scoped per environment rather than globally, because `local` and `dev` (and later `test`/`stage`/`prod`/`demo`) are separate IdP instances — an org named `globo` in two different environments is two different orgs with two different `server_org_id` values, even though the name happens to match.

This is a **local cache, not a source of truth** — same principle as everywhere else in this store. It's populated/refreshed as a side effect of `vl org` commands touching the server, not maintained independently. There is no dedicated `vl org sync` command in this phase; every `add`/`show`/`list`/`update` upserts the relevant row(s) with fresh data and a new `synced_at`, so the cache is "as current as your last command" without extra ceremony.

---

## 4. Commands

### 4.1 `vl org add <name> --display-name <text> [--initial-admin <server-user-id>] [--env <env>] [--as <label>]`
1. Resolve the calling identity per §5 (`--as <label>` / `VL_IDENTITY` / the environment default) and authenticate as it, reusing a cached token when one is valid.
2. `POST /v1/organizations` with `{id: <generated uuid>, name, displayName, initialAdminUserId: <optional>}`. Whether `vl` must derive `name` from `display-name` client-side or the server already does it remains open (§6.1, carried over from the master spec).
3. On success, upsert the local `organization` row.

`--initial-admin`, if supplied, must reference a user that **already exists** — the server grants that user `ORG_ADMIN` and `owner` at creation instead of the caller. This only works for an existing user; it cannot be used to bootstrap a brand-new dedicated admin in the same call, since a user can't be created referencing an org that doesn't exist yet. For that case (the common one — a new org getting its own dedicated admin, e.g. `anchorpoint` + `anchorpoint_admin`), see §4.8, the bootstrap sequence.

If `--initial-admin` is omitted and the authenticating user holds `PLATFORM_ADMIN`, the new org is created with `owner` pointing at that `PLATFORM_ADMIN` and **zero real `ORG_ADMIN` rows** — a deliberate, short-lived state, not an error, meant to be closed immediately via §4.8.

### 4.2 `vl org show <name> [--env <env>]`
Unauthenticated — `GET /v1/organizations?name=`. The `SecurityConfig` gap that previously blocked this (`idp-org-membership-endpoints-spec.md` §5a) has been fixed and confirmed landed. Upserts the local cache row on success, then displays.

### 4.3 `vl org list [--active <bool>] [--env <env>]`
`GET /v1/organizations?active=&page=&limit=&sortBy=&sortOrder=`, matching the `User`/`ServiceAccount` controllers' pagination convention (page-number `nextCursor`). Upserts the local cache for every row returned. Unauthenticated, same as `show` — the endpoint and its `SecurityConfig` carve-out are both confirmed live.

### 4.4 `vl org update <name> [--active <bool>] [--owner <server-user-id>] [--env <env>] [--as <label>]`
Same resolved-identity auth as `add` (§4.1, §5). `PATCH /v1/organizations?name=` (or `/{orgId}` if resolving id first is simpler). Upserts the local cache row on success.

`--owner` is subject to server-side preconditions, not pre-checked by `vl` (per the governing principle — §2 of the master spec): the target must already hold `ORG_ADMIN` on this org, **and** must have both `email` and `phoneNumber` set on their user record. `vl` doesn't validate any of this locally — it sends the request and surfaces whatever the server rejects it with (e.g. a clear error if the target lacks a phone number). This is the final step of the bootstrap sequence in §4.8.

### 4.5 `vl org-members add <org> --user-id <server-user-id> --role <ORG_ADMIN|USER|PLATFORM_ADMIN|KEY_READER> [--env <env>] [--as <label>]`
*(command name updated post-restructure — see the naming note above; originally `vl org members add`.)*

Same resolved-identity auth (§5). `POST /v1/organizations/{orgId}/members`.

`--role KEY_READER` depends on `idp-org-key-rbac-spec.md` landing server-side (the `OrgRole` enum widening) — not usable until then. Once it is, this is also how a designated key-reader account gets set up for `vl org keys` (`vl-org-keys-spec.md`) — no separate command needed, per that document's open item 5.

`--user-id` takes a raw server-side user id, not a local store label — this document was written before a local identity store existed (§1). The identity store has since shipped (Phase 3), but `vl org-members add` has not been revisited to additionally accept `--user <label>` as a resolved-locally convenience; that upgrade remains out of scope here and open (§6).

`vl` does not pre-validate whether a given `--role` makes sense for the target org (per the governing principle — §2 of the master spec) — the server enforces role-specific rules (e.g. `PLATFORM_ADMIN` only within VeritasLock's own org) and rejects otherwise; `vl` just sends the request and surfaces whatever the server returns.

### 4.6 `vl org-members list <org> [--env <env>] [--as <label>]` *(new endpoint — see `idp-org-membership-endpoints-spec.md`)*
`GET /v1/organizations/{orgId}/members` → renders `orgId, userId, role, addedAt` per row. Requires auth server-side (org member or `PLATFORM_ADMIN`) per that spec's §3 — same resolved-identity auth as the other write commands here, even though this one is technically a read, since the endpoint isn't public.

### 4.7 `vl org-members remove <org> --user-id <server-user-id> [--env <env>] [--as <label>]` *(new endpoint — see `idp-org-membership-endpoints-spec.md`)*
`DELETE /v1/organizations/{orgId}/members/{userId}`. The server enforces (per that spec's §4) that this fails with `409` if it would leave the target user with zero org memberships — `vl` does not pre-check this locally, it just surfaces the server's response.

### 4.7a `vl org-members set-role <org> <user-id> --role <ORG_ADMIN|USER|PLATFORM_ADMIN|KEY_READER> [--env <env>] [--as <label>]` *(new endpoint — see `idp-org-member-role-update-spec.md`, confirmed shipped)*
`PATCH /v1/organizations/{orgId}/members/{userId}`. Changes a member's existing role in place — needed because `members add` `409`s on a duplicate `(userId, orgId)` pair rather than updating it, and `members remove` + `members add` doesn't work as a substitute whenever the org is the user's only membership (blocked by the zero-orgs protection). Same resolved-identity auth as its siblings. The server enforces, and `vl` does not pre-check, the last-`ORG_ADMIN` and owner protections that also apply to `removeMember` — a `409` here can mean either "this would leave the org with no real admin" or "this user is the org's owner, reassign ownership first."

*No `vl org delete`.*

### 4.8 Bootstrapping a new organization with a dedicated admin and its event-encryption key

The common real-world case — standing up a brand-new org (e.g. `anchorpoint`) with its own dedicated admin account (e.g. `anchorpoint_admin`) and its event-encryption key, rather than an admin that already exists as a user — can't be done in a single `vl org add` call (§4.1's `--initial-admin` requires an existing user, and a user can't be created referencing an org that doesn't exist yet). It's a four-command sequence instead:

```
# 1. Create the org, authenticating as a PLATFORM_ADMIN (e.g. admin).
#    owner temporarily points at admin; zero real ORG_ADMIN rows on the new org — expected, closed by steps 2-3.
vl org add anchorpoint --display-name "AnchorPoint" --as admin

# 2. Create the dedicated admin, granting ORG_ADMIN on the org just created.
#    Use the admin's real name/email/phone -- these aren't placeholders. A phone number is
#    required before this account can become owner in step 3 (see §4.4, idp-org-admin-safety-spec.md §2.7).
vl usr-acct add <username> --email <email> --org anchorpoint --role ORG_ADMIN --first <First> --last <Last> --phone <number> --as admin

# 3. Hand ownership to the new admin, correcting the temporary state from step 1.
vl org update anchorpoint --owner <new user's server id> --as admin

# 4. Create the org's event-encryption key -- the last step, per vl-org-keys-spec.md.
#    send_events.sh and the node fetch it via the API directly; vl's role ends at creation.
vl org keys create anchorpoint --as <the new admin's identity, or admin>
```

Step 4 is specced in full in `vl-org-keys-spec.md`, a separate document — it uses the same `--as` identity resolution as steps 1–3 here (both post-date the command restructure; see `vl-command-restructure.md` §4).

This mirrors exactly how V20 (the seed-data migration) handles the pre-existing `globo`/`globo_admin` pair — two independently-created records, deliberately stitched together — just performed through the live API instead of a migration, for any org created going forward.


---

## 5. Authentication for write commands

**Superseded — see `vl-command-restructure.md` §4.** As originally specced, this
document was deliberately sequenced before the identity/token-cache machinery
(Phase 3) existed, so `vl org add`, `update`, and `members add`/`list`/`remove`/
`set-role` took credentials directly on every call: a required `--auth-user
<username>` plus a prompted-if-omitted `--auth-password <password>`, spending a
one-off `POST /auth/user/login` scoped to that single request, with nothing
cached to disk.

That interim flow is gone. These commands now resolve the calling identity the
same way every other resource command does:

1. `--as <label>` on the individual command
2. `VL_IDENTITY` env var
3. the environment's default identity

— reusing a cached token from `token_cache` when a valid one exists for that
identity (see `vl-identity-user-spec.md` §7-§8.3), and re-authenticating
otherwise. `vl org show` / `list` remain unauthenticated, unchanged from §4.2-4.3.

---

## 6. Open Items

1. **`vl org add` — client-side vs. server-side `name` derivation:** confirm whether IdP derives `name` from `displayName` server-side, or whether `vl` must replicate the strip/lowercase logic before sending `name` explicitly. (Carried over from the master spec, still unresolved.)
2. ~~`vl org show`/`vl org list` blocked by `SecurityConfig` gap~~ — **Resolved:** fix confirmed landed. Both commands are unauthenticated and functional server-side.
3. ~~`vl org-members add`'s local-write behavior changes once Phase 3 lands~~ — **Resolved.** As originally specced here (under the old `vl org members add` name), this command had no local write. Phase 3 (`vl-identity-user-spec.md` §9.6) retrofitted it to also upsert `org_membership` when `--user-id` matches a locally known identity — shipped, confirmed in `src/vl/commands/org_members.py`.
4. ~~`members list`/`members remove` depend on `idp-org-membership-endpoints-spec.md` landing first~~ — **Resolved:** confirmed implemented, committed, and pushed.
5. ~~§4.8's bootstrap sequence and §4.1's `--initial-admin`/§4.4's `--owner` preconditions depend on `idp-org-admin-safety-spec.md` landing~~ — **Resolved:** confirmed implemented, committed, and pushed. `CreateOrganizationRequest.initialAdminUserId`, the `create` auto-grant/gap fix, `patch`'s phone/email precondition on `ownerId`, and the `phoneNumber` field are all live server-side.
6. **`vl org-members add` has no `--user <label>` convenience** (§4.5) — still open, not addressed by the top-level command split.
