# veritaslock-cli (`vl`) — Phase 6: `vl team`


> **Naming note:** the command surface was later restructured — `vl identity` folded into `vl usr-acct` / `vl svc-acct`, `vl user` → `vl usr-acct`, `vl service-account` → `vl svc-acct`, and `vl org` moved to the `--as` auth model. Behaviour, schema, and rationale below are unchanged; see `vl-command-restructure.md` for the mapping.
>
> **Scope trimmed (later):** `vl-command-restructure.md` §6 **removed the `members` and `ingest-clients` sub-groups** from `vl team` and **dropped `vl team list`'s `<org>` argument** in favour of role-based scoping (§5.3 revised there). `members` was then reintroduced as a top-level **`vl team-member`** command (peer to `vl team`) with just `list` and `add` — see §5.6 and the restructure doc §6. `ingest-clients` (§5.7) is gone for good. The `team` / `team_member` tables and the server endpoints are unchanged.
**Status:** Draft for implementation
**Part of:** the larger `vl-cli-phase1-crud-spec.md` effort, broken out as its own standalone document.
**Depends on:** `vl-env-spec.md` (Phase 1), `vl-org-spec.md` (Phase 2, for the local `organization` cache table this document's `team` table FKs against), `vl-identity-user-spec.md` (Phase 3, for `identity`, the `--as`/cached-token auth flow, and the `org_membership` design pattern this document mirrors), and `vl-service-account-spec.md` (Phase 4, for `identity`/`svc_acct` — team ingest clients populate the same tables Phase 4 established, via a different server endpoint).
**Builds on:** the live `TeamController` on the IdP (`create`, `getById`, `listByOrg`, `update`, `delete`, member and ingest-client sub-resources) and `idp-teams-phase1-spec.md`, the original server-side design document for this API.
**Schema baseline:** current store schema version is `5` (post table-rename migration). This document bumps to `6`.

---

## 1. Scope

This document covers:
- A local `team` cache table, mirroring `organization`'s shape and conventions exactly.
- A local `team_member` join table, mirroring `org_membership`'s shape and conventions exactly — same reasoning, same population strategy (best-effort, populated as a side effect of commands that already know the membership, never authoritative).
- The `vl team` command group: `add`, `show`, `list`, `update`, `delete`, plus `members` and `ingest-clients` sub-groups. *(The two sub-groups were removed later — `vl-command-restructure.md` §6.)*

**No separate `identity_team` cache table** — an earlier, very early draft of this project (before the `organization`/`org_membership` pattern existed) sketched a table by that name for the same purpose `team_member` now serves. `team_member`, designed consistently with everything built since, supersedes that idea; it was never implemented, so there's nothing to migrate away from.

---

## 2. Schema: `team`

| Field | Type | Notes |
|---|---|---|
| environment_name | text | `REFERENCES environment(name) ON UPDATE CASCADE ON DELETE RESTRICT` |
| org_name | text | `REFERENCES organization(environment_name, name)` (composite, via `environment_name` above) |
| name | text | |
| description | text | nullable |
| server_team_id | text | the id IdP assigned — used to build API URLs for this team's sub-resources (members, ingest-clients); the local cache's natural key (`org_name`, `name`) is what a human types, this is what the API needs |
| created_by | text | server user id, audit metadata only |
| created_at | text | ISO8601 |
| synced_at | text | ISO8601 — updated every time this row is touched by a successful `vl team` call |

`PRIMARY KEY (environment_name, org_name, name)`.

Same caching principle as `organization` (Phase 2 §3): local cache, not source of truth, upserted as a side effect of commands touching the server, no dedicated sync command.

## 3. Schema: `team_member`

| Field | Type | Notes |
|---|---|---|
| identity_id | integer | `REFERENCES identity(id) ON DELETE CASCADE` |
| environment_name | text | part of the composite FK below — always equal to the parent identity's own `environment_name` |
| org_name | text | part of the composite FK below |
| team_name | text | part of the composite FK below |
| role | text | `TEAM_ADMIN` / `TEAM_MEMBER` — mirrors the server's `TeamRole` for this membership, as last known locally |
| synced_at | text | ISO8601 |

`PRIMARY KEY (identity_id, environment_name, org_name, team_name)`.
`FOREIGN KEY (environment_name, org_name, team_name) REFERENCES team(environment_name, org_name, name) ON DELETE CASCADE` — unlike `environment`/`organization`'s `RESTRICT`, a `team_member` row has no independent meaning without its parent `team` (same reasoning as `user_acct`/`svc_acct`'s cascade from `identity`), so deleting the local `team` row (§5.5) cleans these up automatically.

Populated the same way `org_membership` is (Phase 3 §4): as a side effect of commands that already know the membership at the moment they run (`team add` for the creator, `team members add`/`set-role` when the target matches a locally known identity), never independently queried or synced. `vl team members list` (§5.7) is the exception — it's the one command that can refresh this cache in bulk, since it returns every member in one call.

---

## 4. Team-ID resolution (used throughout §5)

Every sub-resource operation (members, ingest-clients, `update`, `delete`) needs the server's `teamId`, but the natural thing a human types is `(org, team-name)`. Every command below that needs it follows the same resolution: check the local `team` cache first (`environment_name, org_name, name`) for `server_team_id`; if absent, call `GET /v1/teams?orgId=<resolved>&name=<name>` (upserting the cache on a hit) rather than assuming the cache is complete. Error clearly if no team matches.

---

## 5. `vl team`

Uses Phase 3's real `--as`/`VL_IDENTITY`/default auth mechanism directly throughout — not the one-off `--auth-user`/`--auth-password` flow the original (chronologically first) `vl-org-spec.md` needed, since Phase 3 already exists by the time this document was written (same choice already made in `vl-org-keys-spec.md`).

### 5.1 `vl team add <org> <name> [--description <text>] [--as <label>] [--env <env>]`
`POST /v1/teams` with `{orgId, name, description}`, `orgId` resolved from `<org>` via the local `organization` cache.

**No caller-kind pre-check**, consistent with the governing principle and with `vl service-account add`'s precedent rather than `vl user add`'s exception: team creation is structurally impossible for a service account (the server's `team_member.user_id` FK requires a real user row, not just a policy restriction), but `vl` still doesn't pre-check locally — it resolves whichever identity is active and lets the server's rejection ("Team creation requires a user token") be the answer.

On success: upsert the local `team` row, **and** insert a `team_member` row for the creator with `role = TEAM_ADMIN` — this doesn't need a follow-up API call, since the server's own transaction already guarantees the creator becomes `TEAM_ADMIN` (`idp-teams-phase1-spec.md` §8.1's business rule), so caching it locally alongside the `team` row is safe and free.

### 5.2 `vl team show <org> <name> [--as <label>] [--env <env>]`
`GET /v1/teams?orgId=<resolved>&name=<name>` (not `GET /v1/teams/{teamId}`) — deliberately, since this endpoint's guard (`requireOrgMember`) is broader than `getById`'s (`requireTeamRead`, presumably team-member-specific — exact rule not fully confirmed, §7 item 1), and it's also how `teamId` gets resolved for every other command anyway (§4). This is a design choice, not an oversight: any org member can look up a team's basic info by name, without needing to already be a member of that specific team. Upserts the local cache on success.

### 5.3 `vl team list [--name <filter>] [--as <label>] [--env <env>]`

> **Revised** by `vl-command-restructure.md` §6: the `<org>` argument is gone. Scope follows the caller's role, read from the `orgs` claim in their token (the same claim the IdP's `OrgStandingResolver` reads): a **PLATFORM_ADMIN** entry → every org's teams (page through the public `GET /v1/organizations`, then `GET /v1/teams?orgId=` per org); otherwise one `GET /v1/teams?orgId=` per org in the caller's `orgs` claim. Both render an `org` column. `--name` is passed to each per-org call. Upserts the local cache for every row returned.

### 5.4 `vl team update <org> <name> [--name <new-name>] [--description <text>] [--as <label>] [--env <env>]`
Resolve `teamId` (§4), then `PATCH /v1/teams/{teamId}` with whichever fields are supplied. Upserts the local cache row on success.

### 5.5 `vl team delete <org> <name> [--as <label>] [--env <env>]`
Resolve `teamId` (§4), then `DELETE /v1/teams/{teamId}`. On success, delete the local `team` row — cascades to `team_member` (§3).

### 5.6 Members

> **Reshaped** — `vl-command-restructure.md` §6 replaced the `vl team members` sub-group with a top-level **`vl team-member`** command (peer to `vl team`), keeping only `list` and `add`:
> - **`vl team-member list <team> [--org <name>] [--as <label>] [--env <env>]`** — `GET /v1/teams/{teamId}/members`, team resolved by name across the caller's orgs (§5.3-style scoping). Each `userId` is resolved to a `username` via the team org's `GET /v1/users?orgId=` list, then `vl`'s local cache, then shown raw. Bulk-refreshes the local `team_member` cache.
> - **`vl team-member add <team> <username> [--role TEAM_ADMIN|TEAM_MEMBER] [--org <name>] [--as <label>] [--env <env>]`** — resolve the team (by name) and the user (by username, looked up in the team org's user list — this both maps `username → userId` and enforces "already in the org", which the server also checks in `TeamService.addMember`). Then `POST /v1/teams/{teamId}/members` with `{userId, role?}`. Best-effort local `team_member` upsert if the username matches a known local identity.
> - **No `set-role` / `remove`.** `--role` on `add` is the only role control; re-`add` with a different `--role` also works server-side (it reactivates / re-roles an existing row).
>
> The `PATCH` / `DELETE /v1/teams/{id}/members/{userId}` endpoints still exist server-side. `vl team add` still writes the creator's `TEAM_ADMIN` row (§5.1). The subsection below is the historical `vl team members` design.

- **`vl team members list <org> <team-name> [--as <label>] [--env <env>]`** — resolve `teamId`, `GET /v1/teams/{teamId}/members` → `ListResponse<TeamMemberDto>`. Upserts `team_member` rows for every result — this is the one place this table gets refreshed in bulk (§3).
- **`vl team members add <org> <team-name> --user-id <server-user-id> [--role TEAM_ADMIN|TEAM_MEMBER] [--as <label>] [--env <env>]`** — resolve `teamId`, `POST /v1/teams/{teamId}/members` with `{userId, role}` (`role` optional, defaults server-side to `TEAM_MEMBER` if omitted, per `AddTeamMemberRequest`). On success: upsert a `team_member` row **only if** `--user-id` matches a locally known identity's `server_id` — same best-effort pattern already established for `vl org members add`'s retrofit (Phase 3 §9.6); if the target isn't tracked locally, nothing to attach the membership to, and that's fine.
- **`vl team members set-role <org> <team-name> <user-id> --role <TEAM_ADMIN|TEAM_MEMBER> [--as <label>] [--env <env>]`** — resolve `teamId`, `PATCH /v1/teams/{teamId}/members/{userId}` with `{role}`. Same best-effort local upsert as `add`.
- **`vl team members remove <org> <team-name> <user-id> [--as <label>] [--env <env>]`** — resolve `teamId`, `DELETE /v1/teams/{teamId}/members/{userId}`. If a local `team_member` row exists for that identity/team, delete it.

### 5.7 Ingest clients

> **Removed** — `vl-command-restructure.md` §6 dropped the `ingest-clients` sub-group entirely. The subsection below is historical; the server endpoints are unchanged.

**Team-scoped ingest clients created here become normal local service-account identities**, using the same `identity`/`svc_acct` tables Phase 4 established — even though they're created via a different server endpoint than `vl service-account add`. This keeps the local store internally consistent: an `INGEST_CLIENT` is an `INGEST_CLIENT` locally regardless of which server-side path created it, and it's usable via `--as` for anything else that accepts a service-account identity (including org-key access, `idp-org-key-rbac-spec.md`'s `INGEST_CLIENT` allowlist).

**Real asymmetry, not a bug:** `CreateIngestClientRequest`/`IngestClientCreatedDto` have no `publicKey`/`bootstrapHash` fields at all — team-scoped ingest clients only ever get a symmetric `clientSecret` through this path, never an Ed25519 keypair. `public_key_path`/`private_key_path` stay `NULL` on the resulting `svc_acct` row. `vl service-account get-assertion` (Phase 4 §5.7) will correctly error on one of these — there's no private key to read, and there never will be unless a future server-side change adds keypair support to this endpoint.

- **`vl team ingest-clients list <org> <team-name> [--as <label>] [--env <env>]`** — resolve `teamId`, `GET /v1/teams/{teamId}/ingest-clients` → `ListResponse<IngestClientDto>`. Guarded server-side by `requireIngestClientRead` (`TEAM_ADMIN`, org admin, `SYSTEM`, or the team's own `INGEST_CLIENT` — `vl` doesn't pre-check this, per the governing principle). No local write — this lists service accounts, which `vl service-account list` (Phase 4) already covers for anything locally tracked; this command is a live, authoritative view.
- **`vl team ingest-clients add <org> <team-name> <display-name> [--as <label>] [--env <env>]`** — resolve `teamId`, `POST /v1/teams/{teamId}/ingest-clients` with `{displayName}` → `IngestClientCreatedDto`. On success: create an `identity` row (`kind='SERVICE_ACCOUNT'`, `server_id`/`principal_name` = the returned id, `label` defaulting to a slugified `<team-name>-<display-name>`) and a `svc_acct` row (`org_name` = the team's org, `client_secret_plaintext` = the returned secret, `key_version` from the response, `public_key_path`/`private_key_path` = `NULL`). Print the secret once.
- **`vl team ingest-clients rotate <org> <team-name> <service-account-id> [--as <label>] [--env <env>]`** — resolve `teamId`, `POST /v1/teams/{teamId}/ingest-clients/{serviceAccountId}/rotate` → `IngestClientCreatedDto` (new secret, incremented `keyVersion`). If a local `identity`/`svc_acct` row exists for that service-account id, update `client_secret_plaintext`/`key_version`; otherwise no local write.
- **`vl team ingest-clients delete <org> <team-name> <service-account-id> [--as <label>] [--env <env>]`** — resolve `teamId`, `DELETE /v1/teams/{teamId}/ingest-clients/{serviceAccountId}`. If tracked locally, delete the local `identity` row (cascades to `svc_acct`) — key-directory cleanup is a safe no-op here, since these accounts never had one (§ above).

---

## 6. Migration

Bump `PRAGMA user_version` to `6` (from `5`). Additive only — creates `team` and `team_member`, no changes to any existing table.

---

## 7. Open Items

1. **`TeamAccessGuard.requireTeamRead`'s exact rule was never fully confirmed** — only `requireIngestClientRead` and `requireTeamAdmin` were verified directly against the code earlier in this project. `vl team show`'s design (§5.2, using the broader `listByOrg`/`requireOrgMember` path instead) sidesteps needing to know this precisely, but worth confirming if a future command ever needs `getById` specifically.
2. **`vl team members add`/`set-role`'s local-write behavior depends on the target already being a locally known identity** — same caveat as `vl org members add`'s retrofit (Phase 3 §9.6): if the target user isn't tracked locally, the grant still succeeds server-side, `vl` just can't reflect it in the local cache until something else (e.g. `vl team members list`) refreshes it.
3. **No `vl org keys`-style "last step of bootstrap" cross-reference has been added anywhere for team setup** — unlike org bootstrap (`vl-org-spec.md` §4.8), this document doesn't define a canonical "bootstrap a new team" sequence. Given team creation is a single `vl team add` call (server-side auto-grants the creator `TEAM_ADMIN` in the same transaction, §5.1), there may be nothing more to sequence — flagging in case that assumption is wrong.
