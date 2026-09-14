# veritaslock-cli

`vl` — a Python command-line tool for administering the VeritasLock platform.
It replaces a set of legacy bash scripts with a single, structured CLI
(`git`/`kubectl`-style noun-verb subcommands: `vl org add`, `vl usr-acct add`,
...). It manages users, service accounts, organizations, and teams, and keeps
its own environment definitions and cached credentials in a local SQLite
store rather than relying on environment variables or hand-edited config
files.

## Current status

- **Complete:** user, service-account, organization, and team administration
  (`vl usr-acct`, `vl svc-acct`, `vl org` / `vl org-members`, `vl team` /
  `vl team-member`), plus supporting commands (`vl env`, `vl whoami`,
  `vl history`).
- **In progress:** node administration (`vl node create` / `start` / `stop` /
  `reset`). The local schema for nodes exists, and `vl node create` is under
  active development, but it is not yet wired into the CLI — `vl node` is not
  a usable command today.
- **Planned:** organization-level `start` / `stop` / `reset` (orchestrating
  every node in an org) and network-level `start` / `stop` / `reset`
  (orchestrating across the whole fleet), both layered on top of the
  per-node commands above once those land.

## Development note

This project has been built with AI assistance (Claude Code) to accelerate
development. I'm actively reviewing, extending, and taking ownership of the
codebase as I implement the remaining features by hand.

## Installation

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

This installs the `vl` console script in editable mode along with the dev
tooling (`mypy` in strict mode, `pytest`).

```bash
mypy      # type-check
pytest    # run the test suite
```

## Usage

`vl` seeds a `local` environment in its SQLite store on first run, so basic
commands work with zero setup:

```bash
vl env list                                             # local is there by default
vl whoami                                                # resolved environment / identity

vl org add globo --display-name "Globo Corp"
vl usr-acct add jdoe --email jdoe@acme.com --org globo
vl svc-acct add "Ingest Bot" --role ACCOUNT --org globo  # prints the client secret once
vl team add globo ingest --description "Ingest team"
vl team-member add ingest jdoe
```

Every command supports `--help`, and any incomplete or wrong invocation
prints the relevant help instead of an error. See
[`docs/cli-reference.md`](docs/cli-reference.md) for the full command
reference — configuration variables, identity resolution, and detailed usage
for every noun group.

## Tech stack

- **Python 3.11+**
- [**Typer**](https://typer.tiangolo.com/) — CLI framework (commands, options, help generation)
- [**Rich**](https://rich.readthedocs.io/) — table/console output formatting
- [**httpx**](https://www.python-httpx.org/) — HTTP client for the platform's API
- **SQLite** (via the standard library `sqlite3`) — local store for environments, cached credentials, identities, and history
- [**bcrypt**](https://pypi.org/project/bcrypt/) — client-side password hashing
- [**cryptography**](https://cryptography.io/) — Ed25519 keypair generation and signing for service accounts
- **mypy** (strict mode) and **pytest** (with **respx** for mocking HTTP calls) — type checking and testing
- **Hatchling** — build backend / packaging

## Architecture note

`vl` is an administrative client for the VeritasLock platform, not a part of
the platform's runtime. It talks to the platform's identity/access-management
API to manage organizations, teams, users, and service accounts, and it is
being extended to manage the lifecycle of VeritasLock node processes —
starting, stopping, and resetting them — as a typed, store-backed replacement
for a set of hand-maintained bash scripts. Every environment `vl` knows about
(local, dev, staging, ...) is just a named set of base URLs and cached
credentials in its local store, so the same CLI can operate against any
number of independently running VeritasLock deployments.
