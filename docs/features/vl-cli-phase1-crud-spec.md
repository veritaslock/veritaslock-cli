# veritaslock-cli (`vl`) — Phase 1: User / Service-Account / Team / Org CRUD

**Status:** Draft for implementation
**Supersedes:** the earlier `vl-cli-identity-store-spec.md` draft — scope has narrowed (no `events send` / `start_test_harness.sh` migration in this phase) and grown (full asymmetric + symmetric service-account provisioning, not just secrets).
**Builds on:** the existing `veritaslock-cli` scaffold (Typer/Rich, `src/vl/` layout, noun-verb commands, `vl.lib.config`), and the live `TeamController`, `UserController`, `ServiceAccountController`, and organization endpoints on the IdP.

---

## 1. Scope

**In this phase:** `vl org`, `vl user`, `vl service-account`, `vl team` (including team members and team-scoped ingest clients) — full CRUD against the live IdP APIs, backed by a local SQLite store for credentials and bookkeeping.

**Explicitly deferred to later phases:** migrating `send_events.sh` to `vl events send`, and breaking down `start_test_harness.sh`. Those consume the identities this phase creates but are not built now.

---

## 2. Governing principle: authorization lives in the API, not the CLI

`vl` never pre-checks whether an action is likely to be allowed before making the call. It always sends the request and renders whatever the server returns — including a `403`, formatted cleanly via `vl.lib.output` rather than a raw stack trace. Role/standing logic (`ORG_ADMIN` vs. `PLATFORM_ADMIN`, `TEAM_ADMIN` vs. `TEAM_MEMBER`, the `INGEST_CLIENT` self-read carve-out, etc.) belongs entirely to the server's `*AccessGuard` classes. If that logic changes server-side, `vl` should need zero changes to stay correct — it was never encoding the rule in the first place.

The one place role/kind data appears in the CLI is descriptive: the local store records what an identity *is* (its `role`, its team, its org) purely so `find_identity`-style lookups can help a user pick the right stored credential — not to gate whether a command runs.

---

## 3. Local identity store

### 3.1 Location

Default `~/.config/vl/store.db` (override via `VL_STORE_PATH`), consistent with the existing `VL_ENV`/`VL_API_BASE_URL`/`VL_KAFKA_BOOTSTRAP` pattern in `vl.lib.config`. Created lazily on first use. Add `store.db` and `keys/` (see §3.4) to `.gitignore`.

This is a **local, single-user, plaintext-credential store** — passwords, client secrets, and private key files are all stored unencrypted, matching the trust boundary of the directory structure it replaces. Worth remaining a conscious choice rather than a default drifted into (§8.2).

### 3.2 Schema: `environment`

| Field | Type | Notes |
|---|---|---|
| id | integer | PK, autoincrement |
| name | text | unique, e.g. `local`, `dev-remote` |
| idp_base_url | text | |
| cp_base_url | text | |
| di_base_url | text | |
| kafka_bootstrap | text | nullable |
| is_default | integer | 0/1, at most one row set — backs `vl env use` |
| created_at | text | ISO8601 |

### 3.3 Schema: `identity`

| Field | Type | Notes |
|---|---|---|
| id | integer | PK, autoincrement |
| environment_id | integer | FK → `environment.id` |
| kind | text | `USER` or `SERVICE_ACCOUNT` |
| server_id | text | the id IdP assigned (user id / service-account id) |
| org_id | text | |
| org_name | text | |
| role | text | null for `USER`; `ACCOUNT`/`NODE`/`SYSTEM`/`INGEST_CLIENT` for `SERVICE_ACCOUNT` (`PUBLISHER` retired, never issued locally) |
| label | text | short human-friendly name for picking from a prompt/list |
| username_or_client_id | text | |
| password_plaintext | text | nullable — set only for `USER` (§4.1) |
| client_secret_plaintext | text | nullable — set only for `SERVICE_ACCOUNT` (§5.1) |
| public_key_path | text | nullable — set only once a keypair is provisioned (§5.2) |
| private_key_path | text | nullable — path under `~/.config/vl/keys/<server_id>/private.key`, not raw key bytes in the DB (§3.4) |
| key_version | integer | nullable, mirrors server `keyVersion` |
| created_at | text | ISO8601 |

`UNIQUE (environment_id, label)`.

### 3.4 Key file storage

Private/public keys are written to disk as files (`~/.config/vl/keys/<server_id>/private.key`, `.../public.key`, `chmod 600`/`644` respectively, matching `provision_service_account.sh`'s existing convention) — never embedded as raw bytes in the SQLite DB. The `identity` row only stores the path. This keeps the store schema stable if key storage later needs to move (see §8.3 — production key storage via an org-owned vault is a real future direction; keeping keys out of the DB and behind a path/reference now means that swap doesn't require a schema change later, only a different resolution of `private_key_path`).

### 3.5 Schema: `identity_team`

| Field | Type | Notes |
|---|---|---|
| identity_id | integer | FK → `identity.id` |
| team_id | text | server-assigned team id |
| team_name | text | |
| team_role | text | `TEAM_ADMIN` / `TEAM_MEMBER`; null for `INGEST_CLIENT` (1:1 with a team by construction) |
| synced_at | text | ISO8601 |

`PRIMARY KEY (identity_id, team_id)`. Local cache only, refreshed via `vl team sync` (§7.6) — never a source of truth for authorization (§2).

### 3.6 Module: `vl.lib.store`

Same shape as previously drafted (`add_environment`, `add_identity`, `find_identity`, `set_team_membership`, etc.) — extended with key-file-aware helpers:

```python
def save_keypair(identity_label: str, environment: str, private_key_bytes: bytes, public_key_b64url: str) -> tuple[Path, Path]: ...
def get_private_key_path(identity_label: str, environment: str) -> Path: ...
```

---

## 4. `vl user`

### 4.1 `vl user add <first> <last> --org <org> --role <ADMIN|USER> [--env <env>]`

1. Resolve `org_id` from `--org` via `GET /v1/organizations?name=`.
2. Derive `username` (first-initial + lastname, lowercased) and a plausible `email`, generate a random password, and **bcrypt-hash it client-side** (matching `create_user.sh` — the server only ever receives `passwordHash`, never plaintext).
3. Requires the caller to already hold a **user-token** identity in the store (`--as <label>`, or resolved the same way as other commands) — service accounts, including `SYSTEM`, cannot create users (§ decision this turn). If no suitable stored user identity exists, error clearly rather than silently failing an API call.
4. `POST /v1/users` with `{id: <generated uuid>, username, email, displayName, passwordHash, status: "ACTIVE", mfaEnabled: false, organizations: [{orgId, orgName, role}]}`.
5. On success, store the identity locally (`kind=USER`, `password_plaintext` set) and print the plaintext password once — same one-time-reveal convention as service-account secrets (§5.1), even though technically re-derivable by the user re-running with a new password; the point is the CLI never re-displays a stored plaintext by default.

### 4.2 `vl user show <label> [--reveal-secret]`
`GET /v1/users/{id}`, merged with local metadata. Password is masked unless `--reveal-secret` is passed.

### 4.3 `vl user list [--org <org>] [--status <status>] [--env <env>]`
`GET /v1/users?...` — pass through the existing filters (`status`, `email`, pagination). Renders via `vl.lib.output`, following `nextCursor` if the user asks to page further.

### 4.4 `vl user update <label> [--display-name] [--status] [...] [--env <env>]`
`PATCH /v1/users/{id}`.

### 4.5 `vl user delete <label> [--env <env>]`
`DELETE /v1/users/{id}`, then remove the local `identity` row.

---

## 5. `vl service-account`

Every service account needs **both** a symmetric secret (for `clientId`/`clientSecret` → `/auth/service-account/token`) and an asymmetric Ed25519 keypair (for private-key-signed JWT assertions → `/auth/service-account/authenticate`) — neither is deferrable per your direction. This makes `add` a multi-step flow, mirroring `provision_service_account.sh` exactly.

### 5.1 `vl service-account add <display-name> --role <ACCOUNT|NODE|SYSTEM|INGEST_CLIENT> --org <org> [--secret <value>] [--description <text>] [--env <env>]`

1. Resolve `org_id` from `--org`.
2. Generate a random `clientSecret` if `--secret` not given (`secrets.token_hex(16)`, matching `openssl rand -hex 16`).
3. `POST /v1/service-accounts` with `publicKey: null`, `bootstrapHash: null`, `role`, `orgMembership: {orgId, orgName, role: "ADMIN"}` (mirroring the script — confirm `role` inside `orgMembership` should always be `ADMIN` here, or whether it should be a separate flag).
4. Server returns `ServiceAccountCreatedDto` including a `bootstrapHash`.
5. Generate an Ed25519 keypair locally (raw libsodium format: 64-byte private = 32-byte seed + 32-byte pubkey; 32-byte raw public key), write both to `~/.config/vl/keys/<server_id>/` (§3.4).
6. `PATCH /v1/service-accounts/{id}/public-key?bootstrapHash=<hash>` with the base64url-encoded public key.
7. If step 6 fails, it's safe to retry with the same `bootstrapHash` (it's only nulled server-side on success, per your confirmation) — retry once automatically, then surface the error clearly if it still fails.
8. Store the identity locally with `client_secret_plaintext`, `public_key_path`, `private_key_path` all set. Print the client secret once.

### 5.2 `vl service-account show <label> [--reveal-secret]`
`GET /v1/service-accounts/{id}`, merged with local metadata (key file paths, whether a keypair is provisioned).

### 5.3 `vl service-account list [--org <org>] [--role <role>] [--status <status>] [--env <env>]`
`GET /v1/service-accounts?...`.

### 5.4 `vl service-account update <label> [...] [--env <env>]`
`PATCH /v1/service-accounts/{id}` (note the endpoint also accepts an optional `bootstrapHash` query param for certain updates — pass through if the user supplies one, otherwise omit).

### 5.5 `vl service-account delete <label> [--env <env>]`
`DELETE /v1/service-accounts/{id}` (soft-delete server-side, `status → DELETED`), then remove the local `identity` row **and** the local key directory (matching `deprovision_service_account.sh` — the private key is not portable/recoverable, so if the account needs to exist again, re-provision with a fresh keypair rather than trying to preserve the old one).

### 5.6 JWT-assertion generation

A `vl service-account get-assertion <label> [--aud <url>] [--env <env>]` command, equivalent to `gen_assertion.py`, reading the identity's `private_key_path` and emitting a signed short-lived JWT. Needed for at least `NODE`; confirm scope for other roles once you've researched which ones actually use the assertion path vs. the symmetric token endpoint (§8.1) — implement generically since the signing logic is role-agnostic regardless.

---

## 6. `vl org`

### 6.1 `vl org add <name> --display-name <text> [--env <env>]`
`POST /v1/organizations` with `{id: <generated uuid>, name, displayName}`. Note `name` here is the raw value sent, not derived client-side — confirm whether the IdP itself now performs the strip/lowercase derivation server-side (per the earlier org-naming decision) or whether `vl` should derive `name` from `displayName` itself before sending. **Open — see §8.4.**

### 6.2 `vl org show <name-or-id> [--env <env>]`
`GET /v1/organizations/{orgId}` or `?name=`.

### 6.3 `vl org list [--active <bool>] [--env <env>]` *(new endpoint, to be added server-side — see §7 of prior conversation turn)*
`GET /v1/organizations?active=&page=&limit=&sortBy=&sortOrder=`, matching the `User`/`ServiceAccount` controllers' pagination convention (page-number `nextCursor`, not Team's unused cursor style).

### 6.4 `vl org update <name-or-id> [--active <bool>] [--owner <user-label>] [--env <env>]`
`PATCH /v1/organizations/{orgId}` or `?name=`.

### 6.5 `vl org add-member <org> --user <label> --role <ORG_ADMIN|USER> [--env <env>]`
`POST /v1/organizations/{orgId}/members`.

*No `vl org delete`* — deliberately out of scope (destructive, cascading, no current need).

---

## 7. `vl team`

### 7.1 `vl team add <name> --org <org> [--description <text>] [--env <env>]`
`POST /v1/teams`. Requires a stored **user-token** identity (server enforces `requireUserToken` — a service-account caller, even `SYSTEM`, is rejected because the creator's id must satisfy the `team_member.user_id` FK). `vl` doesn't pre-check this (§2) — it just surfaces the server's rejection clearly if the wrong kind of identity is used.

### 7.2 `vl team show <team-id> [--env <env>]`
`GET /v1/teams/{teamId}`.

### 7.3 `vl team list --org <org> [--name <name>] [--env <env>]`
`GET /v1/teams?orgId=&name=`.

### 7.4 `vl team update <team-id> [--name] [--description] [--env <env>]`
`PATCH /v1/teams/{teamId}`.

### 7.5 `vl team delete <team-id> [--env <env>]`
`DELETE /v1/teams/{teamId}`.

### 7.6 Members
- `vl team members list <team-id> [--env <env>]` — `GET /v1/teams/{teamId}/members`
- `vl team members add <team-id> --user <label> [--role TEAM_ADMIN|TEAM_MEMBER] [--env <env>]` — `POST /v1/teams/{teamId}/members` (role defaults server-side to `TEAM_MEMBER` if omitted)
- `vl team members set-role <team-id> <user-id> --role <role> [--env <env>]` — `PATCH /v1/teams/{teamId}/members/{userId}`
- `vl team members remove <team-id> <user-id> [--env <env>]` — `DELETE /v1/teams/{teamId}/members/{userId}`

### 7.7 Ingest clients
- `vl team ingest-clients list <team-id> [--env <env>]` — `GET /v1/teams/{teamId}/ingest-clients`
- `vl team ingest-clients add <team-id> <display-name> [--env <env>]` — `POST /v1/teams/{teamId}/ingest-clients`, store the returned one-time secret locally
- `vl team ingest-clients rotate <team-id> <service-account-id> [--env <env>]` — `POST .../rotate`, same one-time-reveal handling
- `vl team ingest-clients delete <team-id> <service-account-id> [--env <env>]` — `DELETE .../{serviceAccountId}`

### 7.8 `vl team sync <identity-label> [--env <env>]`
Refreshes `identity_team` rows for a stored identity by querying its current team memberships from IdP (exact endpoint depends on whether a "teams for user X" lookup exists server-side — confirm, or derive via `vl team list --org` cross-referenced against `vl team members list` if no direct lookup exists).

---

## 8. Open Items

1. **JWT-assertion scope beyond `NODE`:** confirmed for `NODE`; other roles' auth mechanism(s) still to be confirmed. `vl service-account get-assertion` (§5.6) is being built generically regardless, since the signing logic doesn't depend on role.
2. **Plaintext local storage:** accepted as consistent with the current directory-based approach; revisit if the store is ever shared/synced across machines.
3. **Production key storage:** out of scope for `vl` itself in this phase — noted as a future direction (org-owned vault/key-store) that the path-based `private_key_path` design (§3.4) should accommodate without a schema change when it comes.
4. **`vl org add` — client-side vs. server-side `name` derivation:** need to confirm whether IdP derives `name` from `displayName` server-side, or whether `vl` must replicate the strip/lowercase logic before sending `name` explicitly.
5. **`vl team sync` data source:** confirm whether a direct "list teams for user" endpoint exists, or whether `vl` needs to derive membership by cross-referencing `vl team list` + `vl team members list` per team.
6. **`orgMembership.role` in `vl service-account add`:** confirm whether this should always be `"ADMIN"` (matching `provision_service_account.sh`) or should be an exposed flag.
