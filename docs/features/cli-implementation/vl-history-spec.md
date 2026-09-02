# veritaslock-cli (`vl`) — `vl history`

**Status:** Implemented.
**Depends on:** the local store (`vl-env-spec.md` §8 schema-migration mechanism).
Adds one table and one top-level command. No network, no auth.

---

## 1. Purpose

A rolling local log of recent `vl` invocations, so an operator can see what they
(or a script) last ran — the CLI equivalent of shell `history`. Convenience only:
it is never read by any other command and never gates behaviour.

## 2. Schema — v7

Migration `_MIGRATIONS[6]` (v6 → v7):

```sql
CREATE TABLE IF NOT EXISTS command_history (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ran_at  TEXT NOT NULL,   -- ISO-8601 UTC
    argv    TEXT NOT NULL    -- rendered command line, e.g. "vl org show globo"
);
```

Additive; existing stores migrate in place on next open.

## 3. Recording

`store.record_command(args)` is called once from `app.main()` for every
invocation with a non-empty argv, **before** the command runs.

- **Redaction.** Values carried by a credential option are replaced with `***`
  before storage — both `--opt value` and `--opt=value` forms. Redacted options:
  `--password`, `--secret`, `--client-id`, `--client-secret`, `--current-password`.
  All other args are `shlex.quote`d.
- **Best-effort.** The whole write is wrapped so a read-only / locked / newer
  store raises nothing — history must never fail the command it is logging.
- **`vl history` is not recorded** (it would only push the entry the user is
  trying to read off the window). Stale `vl history` rows written by an older
  `vl` are also filtered at read time.
- **Ring buffer.** After each insert the table is trimmed to the last
  `_HISTORY_KEEP` (200) rows.

Tests invoke commands through `CliRunner` (args passed directly, not via
`sys.argv`), so the suite does not record its own activity.

## 4. `vl history [-n | --limit <N>]`

Prints the `N` most recent recorded invocations (default 25, `min=1`), **oldest
first** — most recent at the bottom, like shell `history`. Two columns, `when`
(UTC, second precision) and `command`. No index column. Honours `VL_OUTPUT`
(`table` / `json`) like every other command. Empty store → the standard
`(no results)` render.

`store.list_command_history(limit)` returns `list[HistoryEntry(ran_at, command)]`
oldest-first, with the `vl history` filter applied in SQL.
