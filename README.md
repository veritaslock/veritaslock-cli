# veritaslock-cli

`vl` — the unified admin CLI for VeritasLock.

Structured like `git`/`kubectl` with noun-verb subcommands (`vl user add`,
`vl events send`). This tool will progressively replace and consolidate the
bash scripts currently spread across the `veritaslock-node` and
`veritaslock-services` repos (starting with `send_events`, later
`start_test_harness.sh` broken into command groups).

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

Configuration is read from the environment (see `vl/lib/config.py`):

| Variable             | Purpose                                   | Default                  |
| -------------------- | ----------------------------------------- | ------------------------ |
| `VL_ENV`             | Target environment name                   | `local`                  |
| `VL_API_BASE_URL`    | IdP / Control-Plane API base URL          | `http://localhost:8080`  |
| `VL_KAFKA_BOOTSTRAP` | Kafka bootstrap servers for `vl events`   | `localhost:9092`         |
| `VL_OUTPUT`          | Output format: `table` or `json`          | `table`                  |

## Local store

`vl` keeps environment definitions (and, in later phases, credentials) in a
local SQLite store at `~/.config/vl/store.db` (override with `VL_STORE_PATH`).
It is created on first use with a `local` environment already seeded, so
`vl env` works with zero setup.

```bash
vl env list                       # local is there by default
vl env add dev --idp-url http://idp:8080 --cp-url http://cp:8082 --di-url http://di:8083
vl env use dev                    # make dev the default
vl env update dev --cp-url http://cp:9082
vl env show dev
vl env delete dev                 # refused while dev is the default
```

Environment resolution for other commands, most to least specific: `--env <name>`
on the command, then the store's default environment (`vl env use`).

## Organizations

```bash
vl org show globo                      # unauthenticated read; caches the result
vl org list --active                   # unauthenticated read
vl org add globo --display-name "Globo Corp" --auth-user alice
vl org update globo --no-active --auth-user alice
vl org members add globo --user-id <server-user-id> --role ORG_ADMIN --auth-user alice
vl org members list globo --auth-user alice
vl org members set-role globo <server-user-id> --role KEY_READER --auth-user alice
vl org members remove globo --user-id <server-user-id> --auth-user alice
```

Roles: `ORG_ADMIN | USER | PLATFORM_ADMIN | KEY_READER`. `set-role` changes a
member's role in place (`add` rejects an already-member).

Write commands (and `members list`) authenticate with a one-off
`POST /auth/user/login` — pass `--auth-user`, and `--auth-password` is prompted
if omitted. Nothing is cached. `vl org` keeps a local per-environment cache of the
orgs it has seen (`organization` table), refreshed on every successful call.

Standing up a new org with its own dedicated admin is a three-step bootstrap
(the admin account can't exist until the org does):

```bash
vl org add anchorpoint --display-name "AnchorPoint" --auth-user admin
vl user add Ana Reyes --org anchorpoint --role ORG_ADMIN --phone +15555550123 --auth-user admin
vl org update anchorpoint --owner <new user's server id> --auth-user admin
```

If the intended admin is an *existing* user, `vl org add … --initial-admin <user-id>`
does it in one call instead.

## Identities

`vl` stores the identities it authenticates as, per environment. Two tiers:

- **tier 1** — password stored locally (`vl identity import`, `vl user add`); `vl`
  re-authenticates silently as needed.
- **tier 2** — password used once for a token, never written (`vl identity login`);
  when the cached token expires `vl` re-prompts (and fails clearly if there's no TTY).

```bash
# Bootstrap: adopt a pre-existing server account into a reusable tier-1 identity
vl identity import --username admin --org globo --role ORG_ADMIN --label root
vl identity use root                 # make it the default for this environment

# Adopt an existing service account (e.g. SYSTEM) — replaces the VL_SYSTEM_* env vars
vl identity import --kind SERVICE_ACCOUNT --client-id <id> --secret <secret> \
  --role SYSTEM --org veritaslock --label system

vl identity login alice             # tier-2: authenticate as yourself, no stored password
vl identity list
vl identity show root --reveal-secret
```

Service-account identities have no tier-2 mode — the client secret is always
stored, and `vl` acquires their tokens via `/auth/service-account/token` silently.

Command identity resolution, most to least specific: `--as <label>`, then
`VL_IDENTITY`, then the environment's default identity (`vl identity use`).

## Users

```bash
vl user add John Doe --org globo --role USER      # generates + prints a password once
vl user add Ana Reyes --org globo --role ORG_ADMIN --email ana@acme.com --phone +15555550123
vl user show jdoe
vl user list --status ACTIVE
vl user update jdoe --display-name "John Doe" --phone +15555550123 --rotate-password
vl user delete jdoe
```

Every `vl user` command authenticates as the resolved identity (which must be a
`USER`). Passwords are bcrypt-hashed client-side — the server only ever sees the
hash. `--email` defaults to a `first.last@example.com` placeholder; set a real one
(and `--phone`) for any account that may later become an org's owner — the server
requires both before `vl org update --owner` will accept it.

## Service accounts

```bash
vl service-account add "Ingest Bot" --role ACCOUNT --org globo   # prints the secret once
vl service-account show ingestbot --reveal-secret
vl service-account list --role NODE
vl service-account update ingestbot --status SUSPENDED
vl service-account rotate-keys ingestbot                         # new Ed25519 keypair
vl service-account get-assertion ingestbot                       # signed JWT for hand-off
vl service-account delete ingestbot
```

Each account gets a symmetric client secret (for `vl`'s own token acquisition)
and an Ed25519 keypair, written to `<store dir>/keys/<id>/` (`private.key` 600,
`public.key` 644). `get-assertion` prints an EdDSA JWT (300 s) for use elsewhere,
e.g. node-side authentication — it isn't part of `vl`'s internal auth flow.

## Events (stubbed)

```bash
vl events send --file ./payload.json --org acme --count 5
```

## Layout

```
src/vl/
├── app.py            root Typer app; mounts noun sub-apps
├── commands/         one module per noun group
│   ├── env.py             vl env add | list | show | use | update | delete
│   ├── identity.py        vl identity list | show | use | import | login
│   ├── org.py             vl org add | show | list | update | members ...
│   ├── service_account.py vl service-account add | show | list | update | ...
│   ├── user.py            vl user add | show | list | update | delete
│   └── events.py          vl events send
└── lib/              shared helpers used across commands
    ├── config.py     env-var config loading (legacy fallback)
    ├── store.py      local SQLite store (environments, orgs, identities, tokens)
    ├── api.py        httpx client for the IdP API + problem+json errors
    ├── auth.py       token acquisition / caching for authed commands
    ├── passwords.py  client-side password generation + bcrypt hashing
    ├── keys.py       Ed25519 keygen, key-file storage, assertion signing
    ├── roles.py      shared role enums
    └── output.py     table/json output rendering
```
