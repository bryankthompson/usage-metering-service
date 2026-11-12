# ClickHouse mirror (`app/mirror.py`)

An optional, derived copy of the closed hourly rollups in ClickHouse, for analytics queries. It is not the bill: the Postgres
rollup stays the bill and the append-only `usage_events` log stays the source. Nothing in the billing path reads ClickHouse.

## Run it
```bash
CLICKHOUSE_URL=http://clickhouse:8123 docker compose --profile analytics up -d --wait   # adds the clickhouse service
python -m app.rollup                                  # with CLICKHOUSE_URL set, each closed hour is also written there
python -m app.mirror --backfill                       # copy every closed hour that is missing or different
python -m app.mirror --export-actuals actuals.jsonl   # ClickHouse totals, in the file app.reconcile reads
python -m app.reconcile --actuals actuals.jsonl       # exit 1 on drift above --tolerance-bps
```
`CLICKHOUSE_URL` unset or empty turns the mirror off. A plain `docker compose up` does not start or pull ClickHouse. From the
host use `CLICKHOUSE_URL=http://127.0.0.1:8123` (port `CLICKHOUSE_PORT`, default 8123, published on 127.0.0.1 only). The image is
pinned (`clickhouse/clickhouse-server:25.8`), the data is in the named volume `chdata`, and the service has a `/ping` health check. To reset both databases use
`docker compose --profile analytics down -v`, which removes `pgdata` and `chdata` and the network. A plain `docker compose down -v`
on a stack started with the profile removes `pgdata` only; the ClickHouse container keeps running with `chdata` and holds the network.

## The table
`usage_rollups_hourly (tenant_id, model, hour_start DateTime('UTC'), tokens_in, tokens_out, event_count Int64, version UInt64)`,
engine `ReplacingMergeTree(version)`, `ORDER BY (tenant_id, model, hour_start)`. The sorting key is the Postgres primary key, so
writing an hour again replaces its rows instead of adding to them; the row with the highest `version` (the write time in
microseconds) wins. A background merge does the replacing, and it is lazy, so a plain `SELECT` may still show the old and the
new row: query with `FINAL` (the mirror's own reads do). Sums over a `FINAL` read do not double count.

## When it writes, and when that fails
`close_hour` commits to Postgres first. `app.rollup` (`close_due_hours`) closes every due hour first, oldest first, and only then
mirrors the hours it closed, in one pass after the loop, so a ClickHouse that does not answer cannot slow the closing of the
hours (a direct `close_hour` call still mirrors right after its own commit). If `CLICKHOUSE_URL` is set, each hour creates the table
if needed and sends that hour's rollup rows in one `INSERT ... FORMAT JSONEachRow` (5 second timeout per request). Any failure
(refused connection, timeout, a non-2xx answer) is logged at ERROR as `clickhouse mirror failed: hour=<iso> error=...` and changes
nothing else: the close is not rolled back and does not fail, and `app.rollup` exits as before.

In the pass, the first failure that means ClickHouse is unreachable or too slow (refused, timeout, `OSError`) skips the mirror
writes of the hours after it, so a dead ClickHouse costs one timeout per run, not one per hour. Each skipped hour is logged at ERROR
as `clickhouse mirror skipped: hour=<iso> after an earlier failure in this pass`. An error answer for one hour's rows (ClickHouse
is up and refuses them, or the hour is out of range) does not stop the other hours.

A failed or skipped hour stays missing in ClickHouse until `python -m app.mirror --backfill`
reads every closed rollup row from Postgres, compares it with ClickHouse (`FINAL`) and re-sends each hour with any missing or
different row (the whole hour). The comparison needs the mirror's whole state, so `--backfill` does one full `FINAL` read of the
table and groups the Postgres rows once by hour; that full read stays. It is idempotent, prints `mirror: sent N hours, failed M`, and exits 1 if any hour still fails or
ClickHouse cannot be reached.

## Checking it
`--export-actuals FILE` writes one JSON line per (tenant, model, hour) from ClickHouse, the format `--actuals` reads
(`docs/reconcile.md`). `app.reconcile` reports a row on one side only and any column beyond tolerance, and exits 1. If ClickHouse has rows for an hour that is not in `closed_hours`, reconcile would skip them (it counts skipped actuals on stderr only), so
`--export-actuals` names those hours on stderr, still writes the file and exits 1. That also
catches rows that exist only in ClickHouse, which `--backfill` does not look at and does not delete.

## Limits
- Hours from 1970-01-01 to 2106-02-07 06:00 UTC only (ClickHouse `DateTime`); ingest accepts UTC years 1 to 9999. An hour outside
  the range is not written (ClickHouse would wrap it silently): the close logs `clickhouse mirror failed`, and `--backfill` fails on it
  every run and exits 1. The Postgres rollup and the bill are unaffected.
- A derived copy, not the bill; late events are never in it, as in the Postgres rollup.
- Best effort after the close pass, recovered by `--backfill` (run it by hand or from cron; nothing schedules it).
- No authentication on the local ClickHouse (`CLICKHOUSE_SKIP_USER_SETUP`, port on 127.0.0.1 only), single node, no replication, no TLS.
- Memory (measured on main 1dd2039, peak resident size of the rollup process in the container, closing 6000 due hours of 180,000
  rollup rows): 46.2 MB with the mirror off (43.4 MB for a bare import of `app.rollup`) and 122.6 MB with it on, because the rows
  of every closed hour wait for the copy after the close pass. `--backfill` over about 180,000 rows peaked at 311.8 MB (the one
  full `FINAL` read). The mirror-on pass took 54.4 s against 28.5 s with it off.
- Nothing deletes from ClickHouse. The tests use a stand-in HTTP server (`tests/test_mirror.py`); the real server was run by hand
  (the commands above and the memory run above), not in CI, and no larger than that memory run.
