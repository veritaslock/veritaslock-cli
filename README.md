# veritaslock-cli

`vl` — the unified admin CLI for VeritasLock.

Structured like `git`/`kubectl` with noun-verb subcommands (`vl org add`,
`vl usr-acct add`). This tool progressively replaces and consolidates the bash
scripts spread across the `veritaslock-node` and `veritaslock-services` repos.

Any incomplete or wrong invocation — a bare group (`vl`, `vl usr-acct`), an
unknown subcommand (`vl usr-acct blah`), a missing required argument
(`vl usr-acct login`), an unknown option — just prints the relevant `--help` and
exits `0`, exactly as `--help` would. No error box.

## Install

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

This installs the `vl` console script in editable mode along with the dev
tooling (`mypy` in strict mode, `pytest`).

Type-check:

```bash
mypy
```

## Configuration

| Variable         | Purpose                                          | Default                 |
| ---------------- | ------------------------------------------------ | ----------------------- |
| `VL_STORE_PATH`  | Path to the local SQLite store                   | `~/.config/vl/store.db` |
| `VL_IDENTITY`    | Default identity label (see identity resolution) | —                       |
| `VL_OUTPUT`      | Output format: `table` or `json`                 | `table`                 |

Everything else — environments, credentials — lives in the local store, not the
environment.

## Local store

`vl` keeps environment definitions, cached accounts and credentials, and the
identities it authenticates as in a local SQLite store at `~/.config/vl/store.db`
(override with `VL_STORE_PATH`). It is created on first use with a `local`
environment already seeded, so `vl env` works with zero setup.

```bash
vl env list                       # local is there by default
vl env add dev --idp-url http://idp:8080 --cp-url http://cp:8082 --di-url http://di:8083
vl env use dev                    # make dev the default
vl env update dev --cp-url http://cp:9082
vl env show dev
vl env delete dev                 # refused while dev is the default
```

Environment resolution, most to least specific: `--env <name>` on the command,
then the store's default environment (`vl env use`).

The store also keeps a rolling log of the last 200 `vl` invocations:

```bash
vl history            # last 25 commands, oldest first
vl history -n 100     # more
```

Credential option values (`--password`, `--secret`, `--client-id`, …) are
redacted before they're written. `vl history` itself is not logged. History
recording is best-effort — it never fails the command it's logging.

## Identities and authentication

Every command that touches a server resource authenticates as a **resolved
identity** — a user or service account `vl` has cached a credential for. Resolution,
most to least specific:

1. `--as <label>` on the command
2. `VL_IDENTITY` environment variable
3. the environment's default identity (`vl usr-acct use` / `vl svc-acct use`)

```bash
vl whoami            # environment, acting_as, kind, org, role
```

`vl whoami` reports the resolved target: `environment`, `acting_as` (the identity
label), `kind` (`USER` / `SERVICE_ACCOUNT`), `org` (the default org, below), and
`role` (the acting identity's role in that org, from the cached `org_membership`;
`-` for a service account or when there's no single default org).

An identity has an implicit **default org** — a service account's single org, or a
user's sole cached membership. `vl whoami` shows it, and `--org` on
`vl usr-acct add` / `vl svc-acct add` defaults to it.

Getting an identity into the store:

```bash
# Cache an existing user (you must know its password) — stores the password for reuse.
# Locally the account is addressed by its username; there's no label.
vl usr-acct cache --username admin
vl usr-acct use admin

# Authenticate as yourself without storing the password — only the token is cached;
# vl re-prompts when it expires
vl usr-acct login alice

# Cache an existing service account (e.g. SYSTEM) — replaces the VL_SYSTEM_* env vars.
# Service accounts keep a --label (their client id is a UUID, not human-friendly);
# it defaults to the slugified server-side display name.
vl svc-acct cache --client-id <id> --secret <secret> --label system
```

`cache` authenticates **as the account being cached** — you pass *that account's*
own credential, not a caller's, so you can only cache an account whose credential
you have. Caching the same account twice is refused. Drop a cached account locally
(no server call) with `vl usr-acct clear <username>` / `vl svc-acct clear <label>`.

## Organizations

```bash
vl org show globo                     # unauthenticated read; caches the result
vl org list --active                  # unauthenticated read
vl org add globo --display-name "Globo Corp"
vl org update globo --no-active
vl org members add globo --user-id <id> --role ORG_ADMIN
vl org members set-role globo <id> --role KEY_READER
vl org members list globo
vl org members remove globo --user-id <id>
```

Roles: `ORG_ADMIN | USER | PLATFORM_ADMIN | KEY_READER`. `set-role` changes a
member's role in place (`add` rejects an already-member). Write commands and
`members list` authenticate as the resolved identity; `show` / `list` don't.

Standing up a new org with its own dedicated admin is a three-step bootstrap
(the admin account can't exist until the org does):

```bash
vl org add anchorpoint --display-name "AnchorPoint" --as admin
vl usr-acct add Ana Reyes --org anchorpoint --role ORG_ADMIN --phone +15555550123 --as admin
vl org update anchorpoint --owner <new user's server id> --as admin
```

If the intended admin is an *existing* user, `vl org add … --initial-admin <user-id>`
does it in one call instead.

## Users

```bash
vl usr-acct add jdoe --email jdoe@acme.com          # username is the arg; role defaults to USER; --org defaults to your org
vl usr-acct add areyes --org globo --role ORG_ADMIN --first Ana --last Reyes \
    --email ana@acme.com --phone +15555550123 --password 's3cr3t'
vl usr-acct list                                    # cached accounts (local, no server call)
vl usr-acct list --org globo                        # accounts with a membership in globo (local)
vl usr-acct list --remote --status ACTIVE           # server-side users
vl usr-acct list --remote --org globo               # server-side users in globo (server orgId filter)
vl usr-acct show jdoe                               # local view; --remote / --all hit the server
vl usr-acct update jdoe --display-name "John Doe" --phone +15555550123 --rotate-password
vl usr-acct delete jdoe                             # server delete + local clear
```

Both listings carry an `orgs` column (every membership, comma-separated as
`org:role`). On the local listing it's the cached memberships; on `--remote` /
`--all` it's still sourced from vl's local cache — the server user list returns no
membership data — so it reads `-` for accounts vl hasn't cached. The `--remote` /
`--all` listing also has a `cached` column (`yes` / `no`) marking which server
accounts vl has stored locally.

`add` takes the **username** as its argument. `--email` is **required** (the server
rejects a user without a valid address). `--role` is optional and defaults to
`USER`. `--first` / `--last` are optional and only feed the display name. Set
`--phone` too for any account that may later become an org's owner. `--password`
sets a specific password; omit it and a strong random one is generated and printed
once.

Every `vl usr-acct` command authenticates as the resolved identity (which must be
a `USER`). Passwords are bcrypt-hashed client-side — the server only ever sees the
hash.

## Service accounts

```bash
vl svc-acct add "Ingest Bot" --role ACCOUNT --org globo   # prints the secret once
vl svc-acct list                                          # cached (local); --remote / --all for server
vl svc-acct list --org globo                              # accounts in globo (local; add --remote for the server)
vl svc-acct show ingest-bot --reveal-secret               # local view; --remote / --all hit the server
vl svc-acct update ingest-bot --status SUSPENDED
vl svc-acct get-assertion ingest-bot                      # signed JWT for hand-off
vl svc-acct delete ingest-bot
```

Every listing (local and `--remote` / `--all`) shows the account's single `org` and
its `role`. On the server listing `org` is resolved from the row's `orgId` via a
public `GET /v1/organizations/{id}` (cached per run), falling back to the raw id if
the org can't be resolved, and a `cached` column (`yes` / `no`) marks which server
accounts vl has stored locally. `role` is cached locally; `--remote` / `--all` on
`list` or `show` refresh the cached `role` / `key_version` from the server, so an
account cached by an older `vl` (role shows `-`) fills in on its next server view.

An account created with `add` gets a symmetric client secret (for `vl`'s own token
acquisition) and an Ed25519 keypair, written to `<store dir>/keys/<id>/`
(`private.key` 600, `public.key` 644). `get-assertion` prints an EdDSA JWT (300 s)
for use elsewhere, e.g. node-side authentication — it isn't part of `vl`'s internal
auth flow, and doesn't work on an account that has no keypair (e.g. a team ingest
client, which only ever gets a symmetric secret).

## Teams

```bash
vl team add globo ingest --description "Ingest team"   # creator becomes TEAM_ADMIN
vl team show globo ingest
vl team list                                           # scoped by the caller's role — see below
vl team list --name ingest                             # filter by exact name
vl team update globo ingest --name ingestion
vl team delete globo ingestion
```

`vl team list` takes no `<org>` argument — its scope follows the caller's role,
read from the `orgs` claim in their token (the server's own authoritative view):

- **PLATFORM_ADMIN** — every organization's teams (`org` column shows which).
- **ORG_ADMIN / USER** — the teams of the organization(s) the caller belongs to.

`add` / `show` / `update` / `delete` still take `<org> <team-name>`; `vl` resolves
the server team id via its local `team` cache, falling back to
`GET /v1/teams?orgId=&name=`.

## Layout

```
src/vl/
├── app.py            root Typer app; mounts noun sub-apps + `vl whoami` / `vl history`
├── commands/         one module per noun group
│   ├── _shared.py    common typer options, error rendering, org resolution
│   ├── env.py        vl env add | list | show | use | update | delete
│   ├── org.py        vl org add | show | list | update | members ...
│   ├── usr_acct.py   vl usr-acct add | cache | login | use | clear | show | list | update | delete
│   ├── svc_acct.py   vl svc-acct add | cache | use | clear | show | list | update | delete | get-assertion
│   ├── team.py       vl team add | show | list | update | delete
│   ├── whoami.py     vl whoami
│   └── history.py    vl history
└── lib/              shared helpers used across commands
    ├── store.py      local SQLite store (environments, orgs, identities, teams, tokens, history)
    ├── api.py        httpx client for the IdP API + problem+json errors
    ├── auth.py       token acquisition / caching for authed commands
    ├── passwords.py  client-side password generation + bcrypt hashing
    ├── keys.py       Ed25519 keygen, key-file storage, assertion signing
    ├── roles.py      shared role enums
    ├── cli.py        Typer group class: full --help on any usage error
    └── output.py     table/json output rendering
```
