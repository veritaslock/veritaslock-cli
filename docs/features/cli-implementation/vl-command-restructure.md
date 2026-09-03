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
from the phase specs keeps its name under the new noun (`add`, `show`, `list`,
`update`, `delete`, `get-assertion`, …), and its behaviour except where noted
here: `list` / `show` default to the local view (§3), and `usr-acct add` took a
new argument shape (`vl-identity-user-spec.md` §9.1 — `<username>` positional,
`--email` required, `--role` optional defaulting to `USER`, `--first` / `--last`
/ `--password` added). (`svc-acct rotate-keys` was later removed — see
[svc-acct-key-rotation-gap.md](svc-acct-key-rotation-gap.md).)

`usr-acct` is the canonical noun, but **`user` and `user-acct` are accepted as
silent synonyms** — the same sub-app is mounted under all three names, with the
two aliases hidden from `vl --help` and carrying no deprecation notice. `svc-acct`
has no such aliases.

Every command group (root and nested) uses `HelpOnErrorGroup` (`vl.lib.cli`). Any
incomplete or wrong invocation — a bare group, an unknown subcommand
(`vl usr-acct blah`), a leaf command missing a required argument
(`vl usr-acct login`), an unknown option — prints the failing command's full
`--help` to stdout and exits `0`, exactly as if `--help` had been passed. No
usage hint, no red error box, no `Error:` line. `parse_args` handles a group's
own `no_args_is_help` / option errors; `invoke` on the outermost group handles
everything raised deeper (unknown subcommand, a leaf command's argument errors)
as it bubbles up. Only genuine command failures (`report_errors` → `Exit(1)`,
API/auth/store errors) still exit non-zero.

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
- `--org <name>` (`list` only) — filter by org. On the default local listing:
  for `usr-acct`, accounts with an `org_membership` in that org; for `svc-acct`,
  accounts whose `svc_acct.org_name` matches. With `--remote` / `--all` it's
  resolved to the org's id (`GET /v1/organizations?name=`, public) and sent as
  the `orgId` query param that `GET /v1/users` and `GET /v1/service-accounts` now
  accept — for `usr-acct` the server matches any-role membership in that org; for
  `svc-acct`, the account's flat `orgId`.

Both `list`s render an org column in every mode:

- `usr-acct` → `orgs`: every cached `org_membership` for the account, joined as
  `org:role`. `GET /v1/users` returns no membership data, so on `--remote` /
  `--all` this is still the local cache and reads `-` for uncached accounts (a
  dim note says so).
- `svc-acct` → `org`: the account's one org. On `--remote` / `--all` the row's
  `orgId` is resolved to a name via a public `GET /v1/organizations/{id}` (memoised
  per invocation), falling back to the raw id. `svc-acct list` also shows `role`
  (an `svc_acct.role` column, v8).

The `--remote` / `--all` listing for both nouns carries a `cached` column
(`yes` / `no`) flagging which server rows `vl` has a local credential for.
`svc-acct` keeps its `label` column alongside it (the local label, `*` if the
default identity).

`svc-acct list` / `show` on `--remote` / `--all` **backfill the local cache** with
the server-owned fields (`role`, `key_version`) for any account `vl` already has a
credential row for — so an account cached before v8 (role `NULL`) self-heals on
its first server view.

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
acting identity's default org (clear error if that's ambiguous). `vl org` still
takes a positional `<org>`; so do `vl team add` / `show` / `update` / `delete`,
but **`vl team list` dropped it** — see §6.

## 4. `vl org` uses the resolved identity like everything else

`vl org add` / `update` / `members …` dropped `--auth-user` / `--auth-password`
(the interim pre-Phase-3 flow, `vl-org-spec.md` §5) and now authenticate as the
`--as` / `VL_IDENTITY` / default identity, same as `vl usr-acct` / `vl svc-acct` /
`vl team`. `vl org show` / `list` stay unauthenticated.

## 5. `vl events` removed

Out of scope for this version; may return. `commands/events.py` and the
now-orphaned `lib/config.py` were deleted. The `environment.kafka_bootstrap`
column and `vl env --kafka` stay (forward-looking, per `vl-env-spec.md` §3).

## 6. `vl team` trimmed to the team resource itself

The `members` and `ingest-clients` sub-groups were **removed** from `vl team` —
it's now just `add` / `show` / `list` / `update` / `delete`. This supersedes
`vl-team-spec.md` §5.7 entirely and reshapes §5.6. The server endpoints and the
local `team_member` table are untouched.

**`vl team-member` came back as a top-level command, peer to `vl team`** — a
trimmed replacement for the old `vl team members` sub-group, with just two verbs:

- `vl team-member list <team> [--org <name>]` — the team's members, each `userId`
  resolved to a `username` from the team org's user list (then `vl`'s local
  cache, then the raw id). Bulk-refreshes the local `team_member` cache.
- `vl team-member add <team> <username> [--role TEAM_ADMIN|TEAM_MEMBER] [--org <name>]`
  — `POST /v1/teams/{id}/members`. The user must already belong to the team's
  org; `vl` pre-checks that against `GET /v1/users?orgId=` before the call and
  the server enforces it too (`TeamService.addMember`).

Both address the team by **name alone**, resolved across the caller's orgs the
same way `vl team list` scopes (every org for a PLATFORM_ADMIN); `--org`
disambiguates when several match. There is no `set-role` / `remove` — `--role` on
`add` is the only role control for now. `team add` still writes the creator's
`TEAM_ADMIN` row locally (`vl-team-spec.md` §5.1).

**`vl team list` dropped its `<org>` argument** (supersedes `vl-team-spec.md`
§5.3). Scope now follows the caller's role, read from the `orgs` claim in their
token (the same claim the IdP's `OrgStandingResolver` reads, so `vl`'s view
matches the server's authorization view):

- a **PLATFORM_ADMIN** entry (grants admin standing on every org) → list every
  organization's teams: page through the public `GET /v1/organizations`, then
  `GET /v1/teams?orgId=` per org.
- otherwise → one `GET /v1/teams?orgId=` per org in the caller's `orgs` claim
  (any-role membership, so a `KEY_READER`-only org is included — it still passes
  the endpoint's `requireOrgMember` guard).

Both render an `org` column. `--name <filter>` is still accepted and is passed to
each per-org call. A token with no `orgs` claim (a service account) lists nothing.

## 7. `vl org members` split into a top-level `vl org-members`, same shape as §6

The nested `members` sub-group under `vl org` was **removed**; its four verbs
moved unchanged to a new top-level command, peer to `vl org` — the same
restructure §6 already did for `vl team` / `vl team-member`. This supersedes
`vl-org-spec.md` §4.5-§4.7a's command names (behaviour, endpoints, and request
shapes are otherwise unchanged).

- `vl org-members add <org> --user-id <id> --role <role> [--org's --as/--env]`
- `vl org-members list <org>`
- `vl org-members set-role <org> <user-id> --role <role>`
- `vl org-members remove <org> --user-id <id>`

Unlike the `vl team` split, there was no name-resolution logic to carry over —
an org is addressed directly by name (`resolve_org`), not searched for across
the caller's orgs the way a team is — so `vl org-members` is a straight lift of
the old `members_app` commands into `src/vl/commands/org_members.py`, mounted
on the root app as `org-members`. `vl org`'s own commands (`add`, `show`,
`list`, `update`) are unaffected.
