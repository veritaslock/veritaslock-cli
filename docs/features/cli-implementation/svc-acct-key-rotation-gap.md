# `vl svc-acct rotate-keys` — removed; key rotation is not supported

**Status:** The `rotate-keys` subcommand was **removed** on 2026-09-01 (it could
never succeed against a real IdP). It will return once the server work below
lands. Discovered while reviewing `vl svc-acct cache` key handling.

---

## Symptom (what the removed command did)

`vl svc-acct rotate-keys <label>` called:

```
PATCH /v1/service-accounts/{id}   { "publicKey": <new b64url>, "keyVersion": <n+1> }
```

Against a real server this returns **HTTP 400** —
`"Service account does not have a pending bootstrap hash."` The CLI test suite
does not catch this because `respx` mocks the `PATCH` as `200`.

## Root cause 1 — the bootstrap hash is one-time

In `ServiceAccountService` (identity-provider-service), **both** code paths that
can set `publicKey` require a *pending* `bootstrapHash`:

- `update(id, req, bootstrapHash)` — `if (req.publicKey() != null) { … require e.getBootstrapHash() non-null and bcrypt-match … }`
- `provisionPublicKey(id, publicKey, bootstrapHash)` — same guard.

`bootstrapHash` is generated once in `create(...)` and cleared permanently on the
first successful provisioning (`e.setBootstrapHash(null)`). Nothing regenerates
it. So after an account is first activated, there is **no supported way to change
its public key.**

`PATCH /v1/service-accounts/*/public-key` is `permitAll` (no token); the generic
`PATCH /v1/service-accounts/{id}` only requires `authenticated()` — neither
enforces an org role for the key change, but both are blocked by the hash gate
regardless.

## Root cause 2 — single-key model, no rotation overlap

`ServiceAccountEntity` holds a single `publicKey` + a single `keyVersion` int.
Verification (`/auth/service-account/authenticate`) checks the assertion against
that one key. Any key change is therefore a **hard cutover**: the moment the new
key lands, every assertion signed with the old key fails — including ones already
minted and in flight (assertion TTL is 300 s, see `keys.ASSERTION_TTL_SECONDS` /
`gen_assertion.py`).

Zero-downtime rotation needs the server to verify against a **set** of active
keys (old + new, disambiguated by `keyVersion` or a JWT `kid`), retiring the old
key only after a grace window ≥ the assertion TTL. The IdP already does exactly
this for *its own* signing keys via `/.well-known/jwks.json`; it just never
extended the pattern to service-account keys.

---

## What needs to happen

### Server (identity-provider-service) — prerequisite

1. **A way to begin a rotation.** Options, roughly in order of preference:
   - A dedicated `POST /v1/service-accounts/{id}/key-rotations` (ORG_ADMIN of the
     account's org) that returns a fresh one-time bootstrap hash, then reuse the
     existing `PATCH .../public-key` flow to install the new key; or
   - Allow an ORG_ADMIN-authenticated `PATCH /v1/service-accounts/{id}` to set
     `publicKey` directly (no bootstrap hash) once the account is already active.
2. **Dual-key verification (for zero-downtime).** Keep `publicKey_v{n}` valid
   alongside `publicKey_v{n+1}` for a configurable overlap, then drop the old
   one. Requires storing more than one key per account (new table, or a small
   JSON column) and having `authenticate` try each.

### CLI (this repo) — follows the server work

- **Re-add the `rotate-keys` subcommand** (removed 2026-09-01), pointed at
  whatever rotation entry point the server exposes. The removed implementation is
  in git history; `keys.install_keypair()` (the staged-swap primitive it used)
  was kept in `lib/keys.py` for when it returns.
- Two-phase local key swap keyed to the overlap: install the new key, keep the
  old `private.key` as `private.key.v{n}` until the grace window elapses, then
  delete it. `store.update_svc_acct_keys` already versions `key_version`; extend
  the `svc_acct` row (or a sidecar) to track a retiring key + its expiry.
- Add an integration test that runs against a real IdP (or a fake that enforces
  the bootstrap-hash gate) so a mocked `200` can't hide this again.

---

## Interim guidance

- `vl svc-acct add` (first provisioning) works — it uses the bootstrap hash while
  it is still pending. Only *re-keying* an already-active account is broken.
- If a service account's private key is compromised or lost, the current
  practical recovery is **deprovision + re-provision** (`vl svc-acct delete` then
  `vl svc-acct add`), accepting the new client id / server id.
- `vl svc-acct cache --private-key-path` imports an *existing* key; it never
  re-keys the server, so it is unaffected by this gap.

## New dependency: node ownership lock (control-plane-service)

As of the (org_id, org_node) ownership check added to `POST /v1/nodes` and
`PATCH /v1/nodes/{id}/up` (control-plane-service, `node.owner_service_account_id`,
V42 migration), a node's CP row permanently remembers the `client_id` of the
service account that first registered it, and rejects register/markNodeUp calls
from any other service account (`409 NOT_OWNER`) for that same `org_node`.

Because deprovision + re-provision (this doc's interim guidance above) mints a
**new** `client_id`, the recovered node's next `register()` call will now hit
`NOT_OWNER` — the CP row still remembers the *old*, deleted service account as
owner. There is currently no API to clear this; recovering a node after a
lost/compromised key additionally requires a manual DB fix:

```sql
UPDATE node SET owner_service_account_id = NULL
WHERE org_id = '<org-id>' AND org_node = <n>;
```

so the newly-provisioned service account's next register() call claims the row.

Once true key rotation lands (same service account, same `client_id`, new key),
this stops being an issue for the lost/compromised-key case: the node keeps
registering as the account it always was, so `owner_service_account_id` never
needs to change. This is one more reason to prioritize the rotation work above,
not a reason to build a separate reclaim endpoint in the meantime.
