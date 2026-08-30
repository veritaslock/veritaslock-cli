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

## Usage

```bash
vl --help
```

```bash
# Create a user in an org (stubbed)
vl user add alice --org acme

# Show a user (stubbed)
vl user show alice

# Send events from a payload file (stubbed; will replace send_events.sh)
vl events send --file ./payload.json --org acme --count 5
```

## Layout

```
src/vl/
├── app.py            root Typer app; mounts noun sub-apps
├── commands/         one module per noun group
│   ├── env.py        vl env add | list | show | use | update | delete
│   ├── user.py       vl user add | show
│   └── events.py     vl events send
└── lib/              shared helpers used across commands
    ├── config.py     env-var config loading (legacy fallback)
    ├── store.py      local SQLite store (environments; credentials later)
    └── output.py     table/json output rendering
```
