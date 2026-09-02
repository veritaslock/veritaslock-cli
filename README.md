# veritaslock-cli

`vl` — the unified admin CLI for VeritasLock.

Structured like `git`/`kubectl` with noun-verb subcommands (`vl org add`,
`vl usr-acct add`). This tool progressively replaces and consolidates the bash
scripts spread across the `veritaslock-node` and `veritaslock-services` repos.

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
vl usr-acct add John Doe --role USER                # --org defaults to your org
vl usr-acct add Ana Reyes --org globo --role ORG_ADMIN --email ana@acme.com --phone +15555550123
vl usr-acct list                                    # cached accounts (local, no server call)
vl usr-acct list --org globo                        # cached accounts with a membership in globo
vl usr-acct list --remote --status ACTIVE           # server-side users
vl usr-acct show jdoe                               # local view; --remote / --all hit the server
vl usr-acct update jdoe --display-name "John Doe" --phone +15555550123 --rotate-password
vl usr-acct delete jdoe                             # server delete + local clear
```

Every `vl usr-acct` command authenticates as the resolved identity (which must be
a `USER`). Passwords are bcrypt-hashed client-side — the server only ever sees the
hash. `--email` defaults to a `first.last@example.com` placeholder; set a real one
(and `--phone`) for any account that may later become an org's owner — the server
requires both before `vl org update --owner` accepts it.

## Service accounts

```bash
vl svc-acct add "Ingest Bot" --role ACCOUNT --org globo   # prints the secret once
vl svc-acct list                                          # cached (local); --remote / --all for server
vl svc-acct list --org globo                              # cached accounts in globo
vl svc-acct show ingest-bot --reveal-secret               # local view; --remote / --all hit the server
vl svc-acct update ingest-bot --status SUSPENDED
vl svc-acct get-assertion ingest-bot                      # signed JWT for hand-off
vl svc-acct delete ingest-bot
```

An account created with `add` gets a symmetric client secret (for `vl`'s own token
acquisition) and an Ed25519 keypair, written to `<store dir>/keys/<id>/`
(`private.key` 600, `public.key` 644). `get-assertion` prints an EdDSA JWT (300 s)
for use elsewhere, e.g. node-side authentication — it isn't part of `vl`'s internal
auth flow, and doesn't work on an account that has no keypair (e.g. a team ingest
client).

## Teams

```bash
vl team add globo ingest --description "Ingest team"   # creator becomes TEAM_ADMIN
vl team show globo ingest
vl team list globo
vl team update globo ingest --name ingestion
vl team members add globo ingestion --user-id <id> --role TEAM_ADMIN
vl team members set-role globo ingestion <id> --role TEAM_MEMBER
vl team members list globo ingestion
vl team members remove globo ingestion <id>
vl team delete globo ingestion
```

Teams are addressed by `<org> <team-name>`; `vl` resolves the server team id via
its local `team` cache, falling back to `GET /v1/teams?orgId=&name=`. Team ingest
clients are created as normal local `SERVICE_ACCOUNT` identities (symmetric secret
only — no keypair):

```bash
vl team ingest-clients add globo ingestion "edge-01"   # prints the secret once
vl team ingest-clients list globo ingestion
vl team ingest-clients rotate globo ingestion <service-account-id>
vl team ingest-clients delete globo ingestion <service-account-id>
```

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
│   ├── team.py       vl team add | show | list | update | delete | members ... | ingest-clients ...
│   ├── whoami.py     vl whoami
│   └── history.py    vl history
└── lib/              shared helpers used across commands
    ├── store.py      local SQLite store (environments, orgs, identities, teams, tokens, history)
    ├── api.py        httpx client for the IdP API + problem+json errors
    ├── auth.py       token acquisition / caching for authed commands
    ├── passwords.py  client-side password generation + bcrypt hashing
    ├── keys.py       Ed25519 keygen, key-file storage, assertion signing
    ├── roles.py      shared role enums
    └── output.py     table/json output rendering
```
