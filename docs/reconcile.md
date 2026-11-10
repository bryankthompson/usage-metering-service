# Reconcile (`app/reconcile.py`)

A read-only check that the stored hourly rollups agree with the raw event log and, optionally, with totals from
another source. It opens a read-only transaction and never writes.

## What it checks
- **Self-check (always).** For each closed hour (`closed_hours`), it recomputes tokens_in, tokens_out and event count per
  (tenant, model) from `usage_events` where `NOT late`, grouped by the UTC hour of `occurred_at`, with its own SQL (not
  `rollup.AGGREGATE_HOUR`). Drift: a rollup row with no events, events with no rollup row, or any difference in the three numbers.
  A closed hour with no events and no rollup is not drift.
- **Actuals (`--actuals FILE.jsonl`).** One JSON object per line: `tenant_id`, `model`, `hour` (UTC hour boundary, with
  time zone), `tokens_in`, `tokens_out` (integers). Drift per column when `|rollup - actual| * 10000 > tolerance_bps * actual`
  (integers; a difference of exactly the tolerance passes), or when a row exists on one side only. Actuals for an hour that is
  not among the checked closed hours are skipped and counted on stderr.

## Run it
```bash
python -m app.reconcile [--hour 2026-11-10T14:00:00Z | --since ISO] [--actuals FILE.jsonl] [--tolerance-bps 100]
```
`--hour` must be a closed hour. Default tolerance is 100 bps (1% of the actual). One `DRIFT ...` line per drift on stdout,
then `reconcile: hours=N rows=N drift=N`. What ran (scope, file, tolerance) goes to stderr.

## Exit codes
0 clean. 1 drift found. 2 bad argument, unreadable file, or a malformed actuals line (the message names the line; a repeated
tenant/model/hour counts as malformed).

## What it does not check
Open (unclosed) hours, late events (they are excluded by design, so it does not say how many there are), prices or charges,
Stripe, and whether the actuals source is itself right. It does not repair anything.
