# veritaslock-cli (`vl`) — Phase 1: `vl env`

**Status:** Draft for implementation
**Part of:** the larger `vl-cli-phase1-crud-spec.md` effort, broken out as its own standalone document so it can be implemented and tested in isolation. `vl env` is the foundation everything else depends on.
**Builds on:** the existing `veritaslock-cli` scaffold (Typer/Rich, `src/vl/` layout, noun-verb commands, `vl.lib.config`).

---

## 1. Scope

This document covers only the local SQLite store's `environment` table and the `vl env` command group (`add`, `list`, `show`, `use`, `delete`). It does not cover `identity`, `token_cache`, or any of the resource-management commands (`vl user`, `vl service-account`, `vl team`, `vl org`) — those are separate documents that build on this one.

---

## 2. Storage location

Default `~/.config/vl/store.db` (override via `VL_STORE_PATH`), consistent with the existing `VL_ENV`/`VL_API_BASE_URL`/`VL_KAFKA_BOOTSTRAP` pattern in `vl.lib.config`. Created lazily on first use, `chmod 600`. Add `store.db` to `.gitignore`.

## 3. Schema: `environment`

| Field | Type | Notes |
|---|---|---|
| name | text | **PRIMARY KEY** — e.g. `local`, `dev`, later `test`/`stage`/`prod`/`demo` |
| idp_base_url | text | full URL, e.g. `http://localhost:8080` |
| cp_base_url | text | full URL, e.g. `http://localhost:8082` |
| di_base_url | text | full URL, e.g. `http://localhost:8083` |
| kafka_bootstrap | text | nullable — unused by anything in this phase, kept for a future phase (`start_test_harness.sh` migration) rather than added back later via migration |
| is_default | integer | 0/1, at most one row set — backs `vl env use` |
| created_at | text | ISO8601 |

`name` is the primary key deliberately (not a surrogate id), and every table added in later phases that needs to scope a row to an environment references it directly: `environment_name TEXT REFERENCES environment(name) ON UPDATE CASCADE ON DELETE RESTRICT`.

- `ON UPDATE CASCADE`: if an environment is ever renamed, dependent rows follow automatically rather than orphaning.
- `ON DELETE RESTRICT`: `vl env delete` fails outright if *anything* still references the environment (identities, cached tokens, team-membership cache rows, etc., once those tables exist in later phases) — the user must delete those resources first. This is a deliberate safety default: an environment cleanup should never silently cascade-delete credential bookkeeping as a side effect.

## 4. Auto-seeded `local` row

On first use of the store (i.e., whenever any `vl` command finds no `environment` table or an empty one), `local` is created automatically with no user action required:

```
name: local
idp_base_url: http://localhost:8080
cp_base_url: http://localhost:8082
di_base_url: http://localhost:8083
kafka_bootstrap: NULL
is_default: 1
```

This is what makes a fresh install usable with zero setup — `vl env list` and `vl env add` are also both **unauthenticated, local-only operations** (they only touch `store.db`, never call any API), so no admin/bootstrap identity is required to get to a working environment. (Populating an actual usable *identity* against that environment is a separate concern, covered in the `vl identity` document.)

## 5. Commands

All of these are local-only — no network calls, no authentication.

### 5.1 `vl env add <name> --idp-url <url> --cp-url <url> --di-url <url> [--kafka <bootstrap>]`
Inserts a new `environment` row. Fails clearly (not a raw SQLite constraint error) if `name` already exists. Does not set `is_default` — the row is available for use via `--env <name>` or `vl env use <name>`, but doesn't silently take over as the default just by being created.

### 5.2 `vl env list`
Lists all environments (name, three URLs, whether it's the default). Rendered via `vl.lib.output`.

### 5.3 `vl env show <name>`
Detail view of a single environment.

### 5.4 `vl env use <name>`
Sets `is_default = 1` on the named row, clearing it on any other row (single-statement transaction, not two — avoid a window with zero or two defaults if interrupted).

### 5.5 `vl env update <name> [--idp-url <url>] [--cp-url <url>] [--di-url <url>] [--kafka <bootstrap>]`
Partial update — only the flags supplied are changed, same convention as the `PATCH` endpoints elsewhere in this project. Included so a wrong URL can be corrected without deleting and re-adding the environment, which would otherwise become blocked by `ON DELETE RESTRICT` (§3) the moment anything references it in a later phase.

### 5.6 `vl env delete <name>`
Deletes the row. Fails with a clear message (not a raw FK-constraint error) if anything references it — per §3, this will be relevant starting with the `vl identity` phase once `identity` rows exist. In this phase alone, nothing yet references `environment`, so delete is unconditional in practice until later documents add referencing tables.

## 6. Environment resolution

Resolution precedence for "which environment is a command targeting," most to least specific:

1. `--env <name>` flag on the individual command
2. the store's default environment (`environment.is_default = 1`, set via `vl env use`)

If `--env` names an environment that doesn't exist in the store, or (in the degenerate case the store is somehow empty or corrupted) there is no default, fail immediately with a clear error naming the problem and suggesting `vl env list` / `vl env add` — do not attempt to construct a partial environment from anything else. Given `local` is auto-seeded unconditionally on first store-open (§4), the store having zero environments should not occur in practice; this is a defensive error path, not a supported fallback mode.

`VL_ENV`/`VL_API_BASE_URL` (the existing env vars in `vl.lib.config`) are **not** part of this resolution chain. They may still be relevant elsewhere in the codebase wherever `vl.lib.config` is used directly, but `vl env`'s own resolution logic doesn't consult them — mixing "which stored environment" with "an ad hoc single-URL env var" was two different fallback mechanisms doing the same job, and only one is needed given auto-seeding already guarantees a usable default exists.

## 7. Module: `vl.lib.store` (environment portion)

```python
def add_environment(name: str, idp_base_url: str, cp_base_url: str, di_base_url: str, kafka_bootstrap: str | None = None) -> Environment: ...
def list_environments() -> list[Environment]: ...
def get_environment(name: str | None = None) -> Environment: ...  # None -> resolve default per §6; raises a clear, typed error if unresolvable
def set_default_environment(name: str) -> None: ...
def update_environment(name: str, idp_base_url: str | None = None, cp_base_url: str | None = None, di_base_url: str | None = None, kafka_bootstrap: str | None = None) -> Environment: ...  # partial update, only non-None fields change
def delete_environment(name: str) -> None: ...  # raises a clear, typed error on FK restriction, not a raw sqlite3.IntegrityError
def ensure_local_environment_seeded() -> None: ...  # idempotent; called at store-open time
```

## 8. Schema version

Set `PRAGMA user_version = 1` when the store is first created. Every later phase document that adds tables or alters this schema should bump this value and check it at store-open time, so a mismatch (an old `vl` binary against a newer store, or vice versa) is caught explicitly rather than failing confusingly against a schema the code doesn't expect.

## 9. Testing

pytest coverage against a temp SQLite file, covering at minimum:
- Auto-seeding creates exactly one `local` row with `is_default = 1` on first open, and is idempotent (doesn't re-seed or duplicate on subsequent opens).
- `add` rejects a duplicate `name` with a clear error.
- `use` always results in exactly one `is_default = 1` row, never zero or multiple, including after an interrupted/failed transaction.
- `delete` on a referenced environment fails cleanly once a referencing table exists (this specific case may need to wait for the `vl identity` document's tests to be meaningful, but the store function's contract — raise a typed, catchable error rather than propagating `sqlite3.IntegrityError` — should be established here).
