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
│   ├── user.py       vl user add | show
│   └── events.py     vl events send
└── lib/              shared helpers used across commands
    ├── config.py     env/cluster config loading
    └── output.py     table/json output rendering
```
