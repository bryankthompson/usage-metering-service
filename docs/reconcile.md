# Reconcile (`app/reconcile.py`)

A read-only check that the stored hourly rollups agree with the raw event log and, optionally, with totals from
another source. It opens a read-only transaction and never writes.

## What it checks
- **Self-check (always).** For each closed hour (`closed_hours`), it recomputes tokens_in, tokens_out and event count per
  (tenant, model) from `usage_events` where `NOT late`, grouped by the UTC hour of `occurred_at`, with its own SQL (not
  `rollup.AGGREGATE_HOUR`). Drift: a rollup row with no events, events with no rollup row, or any difference in the three numbers.
  A closed hour with no events and no rollup is not drift.
- **Rollups the bill uses but `closed_hours` does not list (always).** Charges reads every row of `usage_rollups_hourly`, so a
  row whose hour is missing from `closed_hours` (the table was edited, or the hour row deleted) is drift: `rollup row but its
  hour is not in closed_hours`. It is not compared with the events; the row itself is the finding. `--hour` and `--since` limit
  it to their hours.
- **Not billed (always printed, information only).** Three lines count events and sum `tokens_in` and `tokens_out` for usage that
  no rollup will bill: `late` (the hour was already closed when the event arrived), `unclosed` (the hour ended over 2 hours ago
  and is still not closed: the rollup is not running, or one hour fails to close) and `future` (dated over 1 hour ahead of the
  clock; a client can date an event year 9999 and it is never closed). A client picks `occurred_at`, so this is how to see how
  much usage sits outside the bill. They do not change the exit code unless `--fail-on-unbilled` is set. `--hour` and `--since`
  limit them by the UTC hour of `occurred_at`. Sums stay exact past the BIGINT range.
- **Actuals (`--actuals FILE.jsonl`).** One JSON object per line: `tenant_id`, `model`, `hour` (UTC hour boundary, with
  time zone), `tokens_in`, `tokens_out` (integers). Drift per column when `|rollup - actual| * 10000 > tolerance_bps * actual`
  (integers; a difference of exactly the tolerance passes), or when a row exists on one side only. Actuals for an hour that is
  not among the checked closed hours are skipped and counted on stderr.

## Run it
```bash
python -m app.reconcile [--hour 2026-11-10T14:00:00Z | --since ISO] [--actuals FILE.jsonl] [--tolerance-bps 100]
                        [--json] [--fail-on-unbilled]
```
`--hour` must be a closed hour. Default tolerance is 100 bps (1% of the actual). One `DRIFT ...` line per drift on stdout,
then the three `not billed: ...` lines, then `reconcile: hours=N rows=N drift=N` (always the last line). `rows` counts the rollup
rows with no `closed_hours` entry too. What ran (scope, file, tolerance) goes to stderr.
```
not billed: late events=1 tokens_in=400 tokens_out=100 (its hour was already closed when it arrived)
not billed: unclosed events=0 tokens_in=0 tokens_out=0 (its hour ended over 2 hours ago and is still not closed)
not billed: future events=0 tokens_in=0 tokens_out=0 (dated over 1 hour ahead)
```
`--json` prints one object instead of the text lines, with the same numbers and the same exit code:
`{"hours": 1, "rows": 2, "drift": ["DRIFT ..."], "not_billed": {"late": {"events": 1, "tokens_in": 400, "tokens_out": 100}, "unclosed": {...}, "future": {...}}}`.

## Exit codes
0 clean. 1 drift found, or (only with `--fail-on-unbilled`) any not-billed event. 2 bad argument, unreadable file, or a malformed actuals line (the message names the line; a repeated
tenant/model/hour counts as malformed).

## What it does not check
Prices or charges (it never rates anything), Stripe, and whether the actuals source is itself right. Open hours are not compared
with the events until they close; their events appear under `unclosed` only once the hour is over 2 hours old, and under `future`
only if dated over 1 hour ahead. Late events are counted but never compared with anything. It does not stop a client from
choosing a late or far-ahead `occurred_at`, and it does not repair anything.
