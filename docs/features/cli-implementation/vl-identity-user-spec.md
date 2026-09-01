# veritaslock-cli (`vl`) — Phase 3: `vl identity` + `vl user`

**Status:** Draft for implementation
**Part of:** the larger `vl-cli-phase1-crud-spec.md` effort, broken out as its own standalone document. Depends on `vl-env-spec.md` (Phase 1) and `vl-org-spec.md` (Phase 2) — the `org_membership` join table introduced here references both `identity(id)` (defined in this document) and `organization(environment_name, name)` (defined in Phase 2).
**Builds on:** the `environment` table from Phase 1, the `organization` table from Phase 2, and the live `UserController` on the IdP.

---

## 1. Scope

This document covers:
- The common `identity` table (kind-agnostic — shared by users and, in a later document, service accounts) and its `USER`-specific extension table, `user_acct`.
- The `org_membership` join table, mirroring the IdP's own `organization` + `user_org_role` split (per Phase 2 §1) — replaces the flat, informational `org_id`/`org_name` columns an earlier draft of this document had put directly on `identity`.
- `token_cache`, the JWT caching layer everything authenticated depends on.
- The `vl identity` command group: `list`, `show`, `use`, `import`, `login`, `forget`.
- The `vl user` command group: `add`, `show`, `list`, `update`, `delete`.
- A small retrofit to Phase 2's `vl org members add` command (§9.6) so it upserts `org_membership` when its target matches a locally known identity — folded into this document's scope rather than left as a later cleanup, since `org_membership` doesn't exist until this phase and the retrofit is small and purely mechanical once it does.

**Deferred to the `vl service-account` document (Phase 4):** the `svc_acct` extension table, `vl identity import --kind SERVICE_ACCOUNT`, and anything involving Ed25519 keypairs. Until that document lands, service-account bootstrapping (e.g. `SYSTEM`) continues via the existing bash scripts' env vars (`VL_SYSTEM_CLIENT_ID`/`VL_SYSTEM_CLIENT_SECRET`) — `vl` does not yet manage those identities.

**Deferred to the `vl team` document (Phase 5):** `identity_team`, the local cache of team membership.

**Still deferred generally:** upgrading Phase 2's write commands (`add`, `update`, `members add`, `members list`, `members remove`) to accept `--as <label>` instead of `--auth-user`/`--auth-password` now that a local identity store exists. The `org_membership` retrofit above is the one piece of Phase 2 cleanup pulled into this document; the `--as` convenience upgrade is not.

---

## 2. Storage

Same `store.db` as Phase 1/2. This document adds tables and references the `organization` table Phase 2 already created.

---

## 3. Schema: `identity` (common)

| Field | Type | Notes |
|---|---|---|
| id | integer | PK, autoincrement |
| environment_name | text | `REFERENCES environment(name) ON UPDATE CASCADE ON DELETE RESTRICT` |
| kind | text | `CHECK (kind IN ('USER', 'SERVICE_ACCOUNT'))` — only `USER` rows are actually created by anything in this document; `SERVICE_ACCOUNT` is reserved for Phase 4 |
| server_id | text | the id IdP assigned (`user.id`) |
| principal_name | text | username (for `USER`; will hold `client_id` for `SERVICE_ACCOUNT` once Phase 4 lands) |
| label | text | short human-friendly name for picking from a prompt/list |
| is_default | integer | 0/1, at most one row per `environment_name` — backs `vl identity use` |
| created_at | text | ISO8601 |

`UNIQUE (environment_name, label)`.

No `org_id`/`org_name` columns on this table — an identity's org relationship(s) live entirely in `org_membership` (§4) now that Phase 2 provides a real `organization` table to join against. This removes the earlier draft's "informational, not exhaustive" caveat entirely: a user's org memberships are now represented properly as a set of rows, matching how the server itself models it, rather than approximated as a single primary org on the identity.

## 4. Schema: `org_membership`

| Field | Type | Notes |
|---|---|---|
| identity_id | integer | `REFERENCES identity(id) ON DELETE CASCADE` |
| environment_name | text | part of the composite FK below — always equal to the parent identity's own `environment_name` (an identity can't hold membership in an org from a different environment) |
| org_name | text | part of the composite FK below |
| role | text | `ORG_ADMIN` / `USER` / `PLATFORM_ADMIN` — mirrors the server's `OrgRole` for this membership, as last known locally |
| synced_at | text | ISO8601 |

`PRIMARY KEY (identity_id, environment_name, org_name)`.
`FOREIGN KEY (environment_name, org_name) REFERENCES organization(environment_name, name) ON UPDATE CASCADE ON DELETE RESTRICT`.

Same caching principle as `organization` itself (Phase 2 §3): this is populated as a side effect of `vl` commands that already know the membership, not queried independently, since there is currently no "list orgs for user X" endpoint to sync against. Populated by:
- **`vl user add`** (§9.1) — the org and role supplied at creation time are known exactly, no ambiguity.
- **`vl identity import`** (§8.4) — read from the login token's `orgs` claim (`[{orgId, role}]`, the user's real server-side memberships baked in at login), not from a flag. `synced_at` reflects the import time; the data is accurate as of then.

`vl org members add` (Phase 2) does not populate this table on its own as originally specced — but as part of this document's scope, it's retrofitted to do so. See §9.6.

## 5. Schema: `user_acct`

| Field | Type | Notes |
|---|---|---|
| identity_id | integer | PK, `REFERENCES identity(id) ON DELETE CASCADE` |
| password_plaintext | text | nullable — `NULL` means tier 2 (§7): the password was never stored, only ever used once to obtain a token |

1:1 extension of `identity` rows where `kind = 'USER'`. `ON DELETE CASCADE` (unlike `environment`'s and `organization`'s `RESTRICT`) is intentional — this row has no independent meaning without its parent `identity`.

## 6. Schema: `token_cache`

| Field | Type | Notes |
|---|---|---|
| identity_id | integer | PK, `REFERENCES identity(id) ON DELETE CASCADE` |
| token | text | cached JWT |
| issued_at | text | ISO8601 |
| expires_at | text | ISO8601 |

Every command that needs to call an authenticated endpoint checks this table first. If a valid (non-expired) token exists for the resolved identity, use it — no re-authentication.

---

## 7. Two identity tiers, and the auth/token flow

- **Tier 1 — credential stored locally.** `user_acct.password_plaintext` is set. Covers dev/test users created via `vl user add`, and any identity explicitly adopted via `vl identity import` (§8.4) with a password supplied for storage. `vl` can silently (re-)authenticate these at any time with no prompt.
- **Tier 2 — credential intentionally not stored.** `user_acct.password_plaintext` is `NULL`. Set up via `vl identity login` (§8.5) — a real operator's own password, used once to obtain a token and immediately discarded, never written to `store.db`. Only the resulting JWT is cached.

**Token acquisition, per invocation:**
1. Resolve the active identity (§8.3 precedence).
2. Check `token_cache` for a valid token. If present and not expired, use it.
3. If absent/expired:
   - **Tier 1:** silently re-authenticate via `/auth/user/login` using the stored password, cache the result, proceed.
   - **Tier 2:** if a TTY is attached, prompt for the password interactively, cache the result, proceed. If no TTY is attached, fail immediately with a clear message ("identity `<label>`'s cached token has expired and no password is stored — run `vl identity login <label>` interactively") rather than hanging on an unanswerable prompt.
4. On a `401` mid-command: same branch as step 3.

---

## 8. `vl identity`

### 8.1 `vl identity list [--env <env>]`
Lists stored identities for an environment (label, kind, principal_name, org memberships via a join against `org_membership`, whether a password is stored). Resolves `--env` per Phase 1's environment resolution.

### 8.2 `vl identity show <label> [--env <env>] [--reveal-secret]`
Detail view, including all cached `org_membership` rows for this identity (org name, role, synced_at). Password masked unless `--reveal-secret`.

### 8.3 `vl identity use <label> [--env <env>]`
Sets `identity.is_default = 1` for that row within its environment, clearing any prior default for the same `environment_name` (single-statement transaction). Resolution precedence for "which identity is running this command," most to least specific:
1. `--as <label>` on the individual command
2. `VL_IDENTITY` env var
3. the environment's default identity (`identity.is_default = 1`)

If none resolve, error clearly rather than guessing.

### 8.4 `vl identity import --kind USER --username <username> --label <label> [--password <password>] [--env <env>]`
Adopts an identity that already exists server-side but wasn't created by `vl` — most importantly, breaks the bootstrap chicken-and-egg: `vl user add` requires an already-resolved user-token identity, but the very first user (e.g. the `admin`/`vlspass` account the current bash scripts assume is pre-seeded) was never created through this CLI.

`import` authenticates **as the account being imported** — `--username`/`--password` are that account's own credential, not a caller's. There is no `--as`. It follows that you can only import an account whose password you know.

1. If `--password` omitted, prompt interactively (hidden input).
2. **Validate the credential and resolve the account**: call `POST /auth/user/login`. If it fails, error clearly and store nothing. The response token's `sub` claim is the server id; its `orgs` claim (`[{orgId, role}]`) is the user's real org memberships.
3. **Refuse a duplicate**: if an `identity` row already exists in this environment for the resolved `server_id` with `kind='USER'`, error naming its current label (`… already imported as '<label>' — use that label, or vl identity forget <label> first`) rather than creating a second row for the same account. This is the guard against "import admin again under a new label."
4. Create the `identity` row (`kind='USER'`) and a `user_acct` row **with the password stored** (`password_plaintext` set — `import` is explicitly for building a reusable tier-1 identity).
5. For each `{orgId, role}` in the token's `orgs` claim: `GET /v1/organizations/{orgId}` (unauthenticated, `permitAll`), upsert the local `organization` cache row, and upsert an `org_membership` row with that real role. `synced_at` = import time. If an org can't be resolved, skip it (note it) — the identity is still adopted; `org_membership` is a cosmetic cache (§4), never load-bearing. **No `--org`/`--role` flags** — the earlier draft's caller-asserted role is gone (resolves open item 4).

### 8.5 `vl identity login <username> [--env <env>]`
The tier-2 path (§7): prompts interactively for a password (hidden input), authenticates against `/auth/user/login`.

**Reuse, not label-based:** before creating anything, check whether an `identity` row already exists in this environment with `kind='USER'` and `principal_name = username` — regardless of what label it was given (so this also catches identities set up via `vl user add` or `vl identity import`, not just prior `login` calls). If found, reuse that row: authenticate, refresh its `token_cache` entry, done. `user_acct` is **never modified** by `login` — if the found row is tier 1 (password already stored) and the password just typed differs, that's not reconciled here; if it's tier 2, it stays tier 2. If no matching row exists, create one: `label = username` (no `--label` flag on this command), `kind='USER'`, resolve `server_id` from the login response, and a `user_acct` row with `password_plaintext = NULL` (tier 2).

Does **not** create an `org_membership` row — this path doesn't ask for or receive org/role information, it only establishes the ability to authenticate. If org context is needed for this identity later, use `vl identity import` instead, or extend this command in a future revision.

### 8.6 `vl identity forget <label> [--env <env>]`
Local-only removal — **no server call**. Deletes the `identity` row and everything that cascades from it (`user_acct`/`svc_acct`, `org_membership`, `team_member`, `token_cache`). The server-side account is untouched — this is the counterpart to `vl user delete` / `vl service-account delete` (which do hit the server) for the case where `vl` simply stored the wrong thing (e.g. an `import` run with the wrong `--label`/`--username`, or a fabricated `--role`). Re-add later with `vl identity import`.

---

## 9. `vl user`

**Authentication for every command in this section:** all five subcommands below (`add`, `show`, `list`, `update`, `delete`) resolve the calling identity per §8.3 precedence (`--as <label>` / `VL_IDENTITY` / environment default) and use the cached-token flow from §7 — there is no unauthenticated path for any user-related endpoint (unlike Phase 2's org reads, which are intentionally public). Each subcommand below accepts `--as <label>` even where not spelled out individually in its signature.

### 9.1 `vl user add <first> <last> --org <org> --role <ORG_ADMIN|USER> [--label <label>] [--email <email>] [--phone <number>] [--env <env>]`

1. Resolve `org_id` from `--org` via `GET /v1/organizations?name=`, and upsert the local `organization` cache row (Phase 2 §3) if needed.
2. Derive `username` (first-initial + lastname, lowercased). `email` defaults to `first.last@example.com` if `--email` is omitted — **this default is a placeholder, not a real address**; it should always be overridden with `--email` for any account that might later become an org's `owner` (see `vl-org-spec.md` §4.8), since a real, reachable email is part of what `owner` is meant to guarantee. `--phone`, if supplied, is sent as `phoneNumber` on creation — required before this account can later be granted `owner` via `vl org update --owner` (`idp-org-admin-safety-spec.md` §2.7), though not required by `add` itself. Generate a random password and **bcrypt-hash it client-side** (the server only ever receives `passwordHash`, never plaintext). `--label`, if omitted, defaults to the derived `username`.
3. Resolve the calling identity per §8.3 precedence. It must be `kind='USER'` — service accounts cannot create users. If resolution yields a service-account identity (once Phase 4 exists) or nothing at all, error clearly before attempting the API call.
4. `POST /v1/users` with `{id: <generated uuid>, username, email, phoneNumber, displayName, passwordHash, status: "ACTIVE", mfaEnabled: false, organizations: [{orgId, orgName, role}]}` — `role` is `ORG_ADMIN` or `USER` per the flag (see §11.1 — this is confirmed correct; the old bash scripts' `"ADMIN"` was simply stale after the team-implementation rename).
5. On success, create an `identity` row (`kind='USER'`), a `user_acct` row with `password_plaintext` set (tier 1 — `vl` generated this password), and an `org_membership` row for the org/role just granted. Print the plaintext password once; it is never displayed again by default.

### 9.2 `vl user show <label> [--reveal-secret]`
`GET /v1/users/{server_id}`, merged with local `identity`/`user_acct`/`org_membership` metadata. Password masked unless `--reveal-secret`.

### 9.3 `vl user list [--org <org>] [--status <status>] [--env <env>]`
`GET /v1/users?...` — pass through existing filters (`status`, `email`, pagination). Follows `nextCursor` if asked to page further. This lists server-side users, not local store rows — cross-reference with `vl identity list --env <env>` (filtered to `kind='USER'`) to see which of them `vl` has stored credentials for.

### 9.4 `vl user update <label> [--display-name <text>] [--status ACTIVE|SUSPENDED] [--email <email>] [--phone <number>] [--mfa | --no-mfa] [--password [<value>]] [--env <env>]`
`PATCH /v1/users/{server_id}` with whichever flags are supplied (partial update), including `phoneNumber` if `--phone` is given. `--username` is **not** exposed in this phase — renaming would desync the local `principal_name`/default-label logic, and there's no pressing need for it yet.

`--password` (value optional — prompts if given with no value, or generates a random one if omitted entirely, matching `add`'s convention): bcrypt-hash client-side, `PATCH passwordHash`. Tier-aware on the local side: if this identity's `user_acct.password_plaintext` was already set (tier 1), update it to the new value — the local record stays in sync with what was just rotated. If it was `NULL` (tier 2), leave it `NULL` — `vl` doesn't start storing a password for an identity that was deliberately tier 2, even though the actual server-side password just changed. The server-side change succeeds either way; only the local caching behavior differs by tier.

### 9.5 `vl user delete <label> [--env <env>]`
`DELETE /v1/users/{server_id}`, then delete the local `identity` row (cascades to `user_acct`, `org_membership`, and `token_cache`).

### 9.6 Retrofit: `vl org members add` now upserts `org_membership`

Phase 2 specified `vl org members add <org> --user-id <server-user-id> --role <...> ...` (`vl-org-spec.md` §4.5) operating purely against the server, with no local write, since `org_membership` didn't exist yet. Now that it does, this command's implementation is extended with one additional step after a successful `POST`:

1. Look up whether any `identity` row in the current environment has `kind='USER'` and `server_id` equal to the `--user-id` just granted membership.
2. If found, upsert an `org_membership` row for that identity (`environment_name`, the org's `name`, and the `role` just granted) — same as if the membership had been established via `vl user add` or `vl identity import`.
3. If not found (the target user isn't one `vl` has a local identity for), do nothing further — there's nothing to attach the membership to locally, and that's fine; it isn't an error.

No changes to the command's flags, server-side behavior, or authentication (still the one-off `--auth-user`/`--auth-password` flow from Phase 2 §5) — this is purely an additional local bookkeeping step layered on top of the existing implementation.

---

## 10. Module: `vl.lib.store` (identity/user portion)

```python
def add_identity(environment: str, kind: Literal["USER", "SERVICE_ACCOUNT"], server_id: str, principal_name: str, label: str) -> Identity: ...
def get_identity(environment: str, label: str) -> Identity: ...
def list_identities(environment: str, kind: Literal["USER", "SERVICE_ACCOUNT"] | None = None) -> list[Identity]: ...
def delete_identity(environment: str, label: str) -> None: ...
def set_default_identity(environment: str, label: str) -> None: ...
def resolve_identity(environment: str, explicit_label: str | None) -> Identity: ...  # implements §8.3 precedence; raises a clear, typed error if unresolvable

def set_user_acct(identity_id: int, password_plaintext: str | None) -> None: ...  # None -> tier 2
def get_user_acct(identity_id: int) -> UserAcct | None: ...

def upsert_org_membership(identity_id: int, environment: str, org_name: str, role: str) -> None: ...
def list_org_memberships(identity_id: int) -> list[OrgMembership]: ...

def get_cached_token(identity_id: int) -> Token | None: ...  # None if absent or expired
def set_cached_token(identity_id: int, token: str, issued_at: datetime, expires_at: datetime) -> None: ...
```

---

## 11. Open Items

1. ~~Org-role enum mismatch~~ — **Resolved:** confirmed to be `provision_service_account.sh` going stale after the org-role enum was renamed during the team implementation (`ADMIN` → `ORG_ADMIN`), not a live server bug. `vl user add`/`vl identity import` sending `ORG_ADMIN`/`USER`/`PLATFORM_ADMIN` is correct as specced; this will get its first real exercise once `vl user add` actually runs against the server. Once `vl user`/`vl service-account` are both live, the stale bash scripts are good candidates for retirement rather than further patching.
2. **`vl identity import` credential validation (§8.4 step 3):** confirm a login-attempt-before-storing is acceptable rather than storing on faith — adds one extra network call to `import` but avoids ever persisting a credential that doesn't actually work.
3. **Command-line `--password` exposure:** `vl identity import --password <value>` puts a secret in shell history / process listing if used non-interactively. Acceptable for local dev bootstrap, but worth a documented caveat rather than silently encouraging the habit.
4. ~~`org_membership.role` staleness for `import`~~ — **Resolved:** `import` no longer takes `--org`/`--role`. It reads memberships from the login token's `orgs` claim (real server data), so there's nothing for the caller to get wrong. The data can still go stale relative to *later* server-side changes (it's a cache with no user-role sync endpoint), but it's never *fabricated* now, and `synced_at` records exactly when it was accurate.
5. **`vl org members add` retrofit (§9.6) needs `vl.lib.store`'s identity-lookup available at the point Phase 2's command runs:** this means Phase 2's code (built earlier, in a separate document/PR) must be revisited to import from this document's store module — worth sequencing the actual implementation work so Phase 3's `identity` table and lookup functions are in place before this retrofit is wired in, even though both land in the same document/PR.
6. ~~`--email`/`--phone` on `vl user add`/`update` depend on `idp-org-admin-safety-spec.md` landing server-side~~ — **Resolved:** confirmed implemented, committed, and pushed.
