# VeritasLock CLI (`vl`) — Authorizing Commands by Role

**Status:** Built on branch `feat/vl-authorization` (`07ec9d1`, `dbe3374`, `cd486a2`, `7230c06`, `2274788`), not pushed or merged. CLI only. 463 tests pass and mypy is clean; 112 tests are new. The mechanism below was checked against the `vl` and control plane code in a report-only pass.
**Companion documents:** `vl-network-spec.md` (§4), `vl-node-spec.md` (§5), `vl-org-phase2-spec.md` (§8), `vl-org-spec.md` (§5, identities and `--as`), `vl-env-spec.md`, `cp-network-reset-spec.md` (§2.2).

---

## 1. Principle

Starting, stopping and resetting a network, an org or a node should be done by someone entitled to. Today only `status` and `reset` at the network level take an operator identity; `start` and `stop` take none, and the org and node commands rely on a node's own credentials or none at all.

**This is a guardrail, not a security boundary.** `start`, `stop` and `reset` on a node are local process and file operations: anyone with shell access can run `verilock` or delete a directory without `vl`. Real enforcement lives where the control plane or the IdP decides, as it does for the network reset endpoint, service accounts and studies. The check in `vl` is still worth having: it keeps the tool consistent, stops people with less privilege doing something by accident, and makes each action attributable to an identity. The documentation must not describe it as security.

---

## 2. Policy

| Commands | Required identity |
|---|---|
| `vl network` `start`, `stop`, `reset`, `status` | `PLATFORM_ADMIN` |
| `vl org` `start`, `stop`, `reset` | `ORG_ADMIN` of that org, or `PLATFORM_ADMIN` |
| `vl node` `start`, `stop`, `reset` | `ORG_ADMIN` of the node's org, or `PLATFORM_ADMIN` |
| `vl org` / `vl node` `list`, `show` | Any identity with access: unchanged. The control plane already scopes what a caller sees (`cp-node-identity-spec.md` §2.5), so `vl` adds no check. |
| Commands that already act on the IdP or control plane as the operator (`org add`, `org update`, `node create`, service accounts, studies) | Unchanged: the IdP and the control plane make the decision. |

`vl network reset` and `status` are decided **by the control plane** (a `PLATFORM_ADMIN` user only; `cp-network-reset-spec.md` §2.2, and `scope=all` in `cp-node-identity-spec.md` §2.5), so `vl` adds no local check for them. The local check (`require_operator`) covers `start` and `stop` at the network level, and `start`, `stop` and `reset` for orgs and nodes.

### 2.1 Today, verified against the code

| Command | `--as` today | After this change |
|---|---|---|
| `vl network start`, `stop` | no | `--as`, `PLATFORM_ADMIN` |
| `vl network reset`, `status` | yes (operator) | unchanged |
| `vl org start`, `stop`, `reset` | no | `--as`, `ORG_ADMIN` of the org or `PLATFORM_ADMIN` |
| `vl node start`, `stop`, `reset` | no | the same |
| `vl org list`, `show`; `vl node list`, `show` | no | unchanged |
| `vl org add`, `update`; `vl node create` | yes (operator at the IdP) | unchanged |

Today `org stop`, `org reset` and `node reset` also call the control plane **as the node's own service account** (to poll `node_is_up`, and for the single `GET /v1/nodes?orgNode=` check, which is skipped for a `NEW` node). That doesn't change: the operator identity gates the command, and the node's service account keeps making those calls.

---

## 3. Mechanism

### 3.1 The token

The state-changing commands take `--as <label>`. Identity resolution already exists (`store.resolve_identity`): `--as`, then `VL_IDENTITY`, then the environment's default identity. `vl` reads the roles from the identity's token. The IdP issues a **user token** like this (`UserTokenService.signUserJwt`):

```
{ "typ": "user", "sub": "<userId>", "username": "...", "email": "...", "mfa_enabled": false,
  "orgs":  [ { "orgId": "org-1", "role": "ORG_ADMIN" }, ... ],
  "teams": [ { "teamId": "team-1", "role": "..." }, ... ] }
```

- A user has **no top-level role.** Roles are per organization: the `orgs` array has one entry per org-role assignment. The IdP code lists `ORG_ADMIN`, `USER` and `PLATFORM_ADMIN`; `vl`'s own `roles.OrgRole` also has `KEY_READER`. Neither `USER` nor `KEY_READER` is enough for a state-changing command.
- Team roles are in `teams`, and removed memberships are left out. **They play no part in these checks.**
- A **service account** token is the exception: it has a top-level `role` (for example `NODE` or `SYSTEM`) plus org and team claims, and no `orgs` array.

### 3.2 The rules

- **The identity must be a user:** the token's `typ` is `user` (or the local identity's kind is user). A service account is **refused** for these commands, `NODE` and `SYSTEM` included, with a message saying so, the same rule as the network reset endpoint. Its token has no `orgs` array, so a role check alone would also fail it, but the explicit check says why. A node's own service account only authenticates the node process, never the command that starts it.
- **Platform admin:** any `orgs` entry whose `role` is `PLATFORM_ADMIN`, whatever its `orgId`. This is exactly how the control plane decides (`NodeAccessGuard.hasPlatformAdminClaim`), and how `team list` and `team_member` already decide in `vl`, so the two agree.
- **Admin of org X:** an `orgs` entry whose `orgId` equals the `server_org_id` of X and whose `role` is `ORG_ADMIN`; a platform admin also satisfies it. `server_org_id` comes from the local organization row, which is always set (rows are only created from an IdP organization). An org that isn't cached locally fails before any role check with the existing "Run `vl org show <name>` first" error.

### 3.3 Reading the token

`auth.get_token` returns the identity's cached token if it is still valid (it counts a token as expired 30 seconds early), with no network call. `api.decode_jwt_payload` then decodes the payload locally **without verifying the signature**. The check is a guardrail, and the IdP and control plane verify tokens where it matters. The roles are as of when the token was issued, so a role revoked a moment ago may still pass; the real decisions are made elsewhere.

There is no refresh token. When the cached token has expired, `vl` logs in again before the check: silently for a service account or a user whose password is stored, and with an **interactive password prompt** otherwise (an `AuthError` if there is no terminal). So a state-changing command can now prompt for a password where it never did before. Open item 2.

### 3.4 Order

Resolve the identity (`--as`, then `VL_IDENTITY`, then the environment default); look up the org (an org that isn't cached locally fails with "Run `vl org show <name>` first" before any role check); refuse a service account, judged from the local identity's kind, **before any token is fetched**; read the current token; require `typ` to be `user`; then check the role. Only then does anything else happen: prompts, process checks, signals, file operations, control-plane calls. A refusal is a `CliError` with no side effects. The messages end with a full stop, like `vl`'s other errors, for example `'<label>' is not an admin of '<org>': this needs ORG_ADMIN for the org, or PLATFORM_ADMIN.`; the wording for a service account, an undecodable token and a token with no `typ` is in `require_operator`. The one thing that can run first is the login that fetching an expired token triggers (§3.3).

### 3.5 Passing the identity down

`vl network start/stop/reset` check `PLATFORM_ADMIN` once, then pass the identity to each `vl org …` call, which passes it to each `vl node …` call, the same way `--include-snapshots` is passed (`vl-node-spec.md` §5.4). Each level re-checks, which costs nothing and keeps each command safe on its own; a platform admin satisfies every org-level check. The node process still authenticates as its own service account. The operator identity only gates the command.

### 3.6 Implementation notes

- One shared helper, in `_shared.py` next to `caller_orgs_claim`: `is_platform_admin(claims)`, `is_org_admin(claims, server_org_id)` and a `require_operator(...)` that applies §3.2 and §3.4. Today there is no such helper: `team list` and `team_member` each repeat `any(e.get("role") == "PLATFORM_ADMIN" …)` inline, and `usr_acct._orgs_claim` duplicates `caller_orgs_claim`. Moving those onto the shared helper is worthwhile but is its own commit.
- Don't use the locally stored `org_membership` rows for the check: they are written at login or cache time and may be stale. Read the current token.
- The "no default identity" error message still names the old command `vl identity use`. It should say `vl usr-acct use` or `vl svc-acct use`.

---

## 4. What changes elsewhere

- `vl-network-spec.md` §4 says two of `vl network`'s four verbs take no `--as`, and `vl-node-spec.md` §5.3 and `vl-org-phase2-spec.md` §8 say `stop` has none. Each gets `--as` and these rules. They are marked with a pointer here until they are rewritten.
- Tests. The twelve `network reset` tests already resolve an identity, but its cached token is the plain string `caller-jwt`, which cannot be decoded; each needs a decodable user token (`tests/_helpers.fake_jwt`, and the `_jwt(orgs)` setup in `test_team_cli.py`). **There are currently no tests at all for `network start/stop` or for `org` and `node` `start/stop/reset`**, so nothing breaks, but there is also nothing protecting these commands. Tests for their ordinary behavior should be added alongside the authorization tests, not only the refusals. Each needs a cached org row with a `server_org_id`, a caller with a decodable token, and, for `stop` and `reset`, node rows plus a node service account with a cached token for the `node_is_up` call.
- A default identity per environment keeps the extra typing down in a development loop.
- **Operational impact:** a script that runs `vl network start` or `stop`, or an org or node `start`, `stop` or `reset`, without `--as` now needs a default identity with the right role (set with `vl usr-acct use <label>`). A service account can't run them at all (open item 3). This includes `reset-demo-environment` and any unattended use.

---

## 5. Open items

1. ~~How the control plane identifies a platform admin~~ — **Resolved:** any `orgs` entry with `PLATFORM_ADMIN`, and `typ` must be `user` (`NodeAccessGuard.isPlatformAdminUser`). `vl` already uses the same rule.
2. ~~How `vl` holds a token~~ — **Resolved** (§3.3), with a consequence to accept or avoid: a command may now prompt for a password when the token has expired and the identity has none stored.
3. **Unattended use** (a systemd unit, a CI job). Not designed. A node's own service account isn't an org admin, so it needs a decision when the need arises. A stored-password user identity works without a terminal; one without does not.
4. ~~Whether to share one helper~~ — **Resolved** (§3.6): yes.
5. ~~`vl node list` and `show` did not match `vl-node-spec.md` §5.5 and §5.6~~ — **Resolved:** they were not built; they are now (five commits on `feat/vl-env`, merged).
6. **`vl node reset` checks the control plane before its prompt,** while an earlier draft of `vl-node-spec.md` §5.4 had the prompt first. The code's order is better (the user is never asked to confirm something the check will refuse), and the spec now says so.
7. **A network reset re-checks the role after the control plane has been wiped.** The role rule is the same "`PLATFORM_ADMIN` user" on the same token, so the check can't refuse a caller the control plane accepted. The one way it could fail is a token that expires mid-run, which needs a fresh login; the reset is idempotent, so running it again finishes the job.
