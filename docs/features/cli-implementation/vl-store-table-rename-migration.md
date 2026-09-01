# veritaslock-cli (`vl`) — Table Rename: `user_credential` → `user_acct`, `service_account_credential` → `svc_acct`

**Status:** Draft for implementation
**Not a design change.** Both tables are already implemented and live in `store.db` (Phase 3 and Phase 4 are confirmed shipped) — this is a rename for naming-convention consistency, not new schema. `vl-identity-user-spec.md` and `vl-service-account-spec.md` have already been updated to use the new names throughout; this document covers the migration needed to actually get there from what's currently deployed.

---

## 1. Rationale (for context, not re-litigating)

`_credential` was harder to type than necessary and implied more precision than the tables actually had (`svc_acct` also holds `org_name`, not just credential material). The new names are deliberately abbreviated and symmetric (`user_acct`/`svc_acct`) rather than exactly matching the server's `user`/`service_account` table names — a conscious choice for uniformity within the local store over an exact mirror of server naming.

## 2. Migration

Bump `PRAGMA user_version` to `4` (from `3`, confirmed as the store's current version after Phase 4). At store-open time, if the current version is `3`:

```sql
ALTER TABLE user_credential RENAME TO user_acct;
ALTER TABLE service_account_credential RENAME TO svc_acct;
PRAGMA user_version = 4;
```

**`ALTER TABLE ... RENAME TO`, not drop-and-recreate** — this must preserve every existing row exactly as-is (passwords, client secrets, key paths, key versions, org names already stored for real accounts on this machine). SQLite's `RENAME TO` updates the table itself in place; no data migration logic is needed beyond the rename statements themselves, and foreign keys referencing these tables by name (`user_acct`/`svc_acct` as PK targets for nothing else references them by name, only `identity(id)` as their own FK — confirm no other table has a hardcoded FK reference to the old names before assuming this is purely mechanical).

Idempotent per the same convention as every other migration in this store (Phase 1's `ensure_local_environment_seeded`-style check, Phase 3/4's additive-table checks): if `user_version` is already `≥ 4`, skip — don't attempt the rename twice.

## 3. Code changes

Every reference to `user_credential`/`service_account_credential` in `vl.lib.store` (table names in SQL, and the function names `set_user_credential`/`get_user_credential` → `set_user_acct`/`get_user_acct`, plus the return type `UserCredential` → `UserAcct`) needs updating to match. `vl-identity-user-spec.md` and `vl-service-account-spec.md` already reflect the new names — treat those documents as authoritative for what the post-migration code should look like.

## 4. No other tables affected

Only these two extension tables are being renamed. `environment`, `organization`, `identity`, `org_membership`, `token_cache` keep their current names — this migration doesn't extend the abbreviation convention to anything else.
