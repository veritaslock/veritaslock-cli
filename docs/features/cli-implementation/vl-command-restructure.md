# veritaslock-cli (`vl`) — Command Restructure

**Status:** Implemented.
**Not a design change** to what the commands *do* — a rename of the command surface
for consistency, plus one auth-model unification. The earlier phase specs
(`vl-identity-user-spec.md`, `vl-service-account-spec.md`, `vl-org-spec.md`,
`vl-team-spec.md`) still hold for behaviour, schema, and rationale; only the
command *names* and `vl org`'s auth flow changed. This document is the mapping.

---

## 1. `vl identity` is gone; its verbs move under the two account nouns

`vl identity` was kind-agnostic (a stored credential is "something `vl` can
authenticate as," user or service account). In practice users think in users and
service accounts, so it folded into those.

| old | new |
|---|---|
| `vl identity import --kind USER …` | `vl usr-acct cache --username <u>` |
| `vl identity import --kind SERVICE_ACCOUNT …` | `vl svc-acct cache --client-id <id> --secret <s> --label <l>` |
| `vl identity login <username>` | `vl usr-acct login <username>` |
| `vl identity use <label>` | `vl usr-acct use <username>` / `vl svc-acct use <label>` |
| `vl identity forget <label>` | `vl usr-acct clear <username>` / `vl svc-acct clear <label>` |
| `vl identity list` | folded into `vl usr-acct list` / `vl svc-acct list` (see §3) |
| `vl identity show <label>` | folded into `vl usr-acct show <username>` / `vl svc-acct show <label>` (see §3) |

- `import` → **`cache`**: it stores a *copy* of the account locally (identity row +
  credential + cached memberships), not a pointer — `cache` says that; `import`
  was too generic and `link` implied a reference.
- `forget` → **`clear`**: pairs with `cache`.
- `login` stays: it *is* "authenticate to establish a token," and unlike `cache`
  it does **not** persist the password. `vl svc-acct` has no `login` (service
  accounts have no token-only tier).
- `--kind` disappears — the noun carries it.
- The `identity` table (and `user_acct` / `svc_acct` / `org_membership` /
  `token_cache`) are unchanged. The user just never types "identity" any more.

## 2. `vl user` → `vl usr-acct`, `vl service-account` → `vl svc-acct`

3-char prefixes on both, symmetry with the store table names. Every subcommand
from the phase specs keeps its name and behaviour under the new noun (`add`,
`show`, `list`, `update`, `delete`, `get-assertion`, …). (`svc-acct rotate-keys`
was later removed — see
[svc-acct-key-rotation-gap.md](svc-acct-key-rotation-gap.md).)

**No `--label` for users.** A user's local handle is its **username** — `usr-acct`
commands take a `<username>` argument, and `cache` / `add` set `identity.label`
to the username automatically. `svc-acct` keeps `--label` because a service
account's `principal_name` is its client-id UUID, which isn't a usable handle. The
`identity` table's `label` column is unchanged.

## 3. `list` / `show` default to the local view

`vl usr-acct list` / `vl svc-acct list` and `… show <label>` now show the **local
cached** view by default — no server call. Flags:

- `--remote` — the server-side listing / record instead (authenticated).
- `--all` — the server record merged with local metadata.

`vl identity list`'s cross-kind "what can I `--as`?" view is replaced by top-level
**`vl whoami`** plus the two per-kind local `list`s.

### `vl whoami` output

A single row (no server call), fields:

| field | value |
|---|---|
| `environment` | the resolved target environment |
| `acting_as` | the resolved identity's label (or a "(none — …)" hint if nothing resolves) |
| `kind` | `USER` / `SERVICE_ACCOUNT` (`-` if none) |
| `org` | the identity's **default org** (§3a), or a hint: `(none or ambiguous — pass --org)` for a user, `(unknown — re-cache the account)` for a service account |
| `role` | the identity's role in that org, from the cached `org_membership` row; `-` for a service account or when there's no single default org |

`--as <label>` resolves as if that identity were selected. There is **no**
`idp_base_url` field (dropped — it's environment config, not identity state).

## 3a. Default org

An identity has an implicit **default org**: a `SERVICE_ACCOUNT`'s single org, or a
`USER`'s sole cached `org_membership` (none if it has zero or several). Implemented
as `store.default_org_for_identity()`. `vl whoami` shows it, and `--org` on
`vl usr-acct add` / `vl svc-acct add` is now optional — omitted, it uses the
acting identity's default org (clear error if that's ambiguous). `vl org` /
`vl team` still take a positional `<org>`.

## 4. `vl org` uses the resolved identity like everything else

`vl org add` / `update` / `members …` dropped `--auth-user` / `--auth-password`
(the interim pre-Phase-3 flow, `vl-org-spec.md` §5) and now authenticate as the
`--as` / `VL_IDENTITY` / default identity, same as `vl usr-acct` / `vl svc-acct` /
`vl team`. `vl org show` / `list` stay unauthenticated.

## 5. `vl events` removed

Out of scope for this version; may return. `commands/events.py` and the
now-orphaned `lib/config.py` were deleted. The `environment.kafka_bootstrap`
column and `vl env --kafka` stay (forward-looking, per `vl-env-spec.md` §3).
