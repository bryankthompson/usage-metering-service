"""Reconcile: a read-only check that the stored hourly rollups can be trusted.

Self-check (default): for every closed hour, recompute the per-(tenant, model) sums from `usage_events`
(`NOT late`, UTC hour of `occurred_at`) and compare them with `usage_rollups_hourly`. The recompute below is its
own SQL on purpose and does not import `rollup.AGGREGATE_HOUR`: two implementations make a bug in either show as drift.

`--actuals FILE.jsonl`: also compare the rollups with totals reported by another source, within `--tolerance-bps`.

CLI: `python -m app.reconcile`. Exit 0 clean, 1 drift found, 2 bad argument or malformed actuals line.
Integer arithmetic only. Details in docs/reconcile.md.
"""
import argparse
import json
import sys
from datetime import datetime, timezone

import psycopg

from app import db

CLOSED_HOURS = "SELECT hour_start FROM closed_hours WHERE (%(since)s::timestamptz IS NULL OR hour_start >= %(since)s) AND (%(only)s::timestamptz IS NULL OR hour_start = %(only)s) ORDER BY hour_start"

# Straight from the raw log, grouped by the UTC hour of the event.
RECOMPUTE = """
SELECT tenant_id, model, date_trunc('hour', occurred_at, 'UTC') AS hour_start,
       sum(tokens_in)::bigint AS tokens_in, sum(tokens_out)::bigint AS tokens_out, count(*) AS event_count
FROM usage_events
WHERE NOT late AND date_trunc('hour', occurred_at, 'UTC') = ANY(%(hours)s)
GROUP BY 1, 2, 3
"""

STORED = "SELECT tenant_id, model, hour_start, tokens_in, tokens_out, event_count FROM usage_rollups_hourly WHERE hour_start = ANY(%(hours)s)"

ACTUAL_FIELDS = ("tenant_id", "model", "hour", "tokens_in", "tokens_out")


class BadInput(Exception):
    pass


def parse_time(text: str, what: str) -> datetime:
    try:
        ts = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        raise BadInput(f"{what}: not an ISO 8601 timestamp: {text!r}") from None
    if ts.tzinfo is None:
        raise BadInput(f"{what}: needs a time zone: {text!r}")
    return ts.astimezone(timezone.utc)


def parse_hour(text: str, what: str) -> datetime:
    ts = parse_time(text, what)
    if ts != ts.replace(minute=0, second=0, microsecond=0):
        raise BadInput(f"{what}: not on a UTC hour boundary: {text!r}")
    return ts


def read_actuals(path: str) -> dict[tuple[str, str, datetime], tuple[int, int]]:
    """Parse the JSONL file. Any bad line raises BadInput naming its line number."""
    actuals = {}
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError as e:
        raise BadInput(f"cannot read {path}: {e.strerror}") from None
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        where = f"{path} line {number}"
        try:
            row = json.loads(line)
        except ValueError:
            raise BadInput(f"{where}: not JSON") from None
        if not isinstance(row, dict) or set(row) != set(ACTUAL_FIELDS):
            raise BadInput(f"{where}: needs exactly the keys {', '.join(ACTUAL_FIELDS)}")
        for key in ("tenant_id", "model", "hour"):
            if not isinstance(row[key], str) or not row[key]:
                raise BadInput(f"{where}: {key} must be a non-empty string")
        for key in ("tokens_in", "tokens_out"):
            if type(row[key]) is not int or row[key] < 0:
                raise BadInput(f"{where}: {key} must be an integer of 0 or more")
        key = (row["tenant_id"], row["model"], parse_hour(row["hour"], f"{where}: hour"))
        if key in actuals:
            raise BadInput(f"{where}: repeats tenant_id, model and hour of an earlier line")
        actuals[key] = (row["tokens_in"], row["tokens_out"])
    return actuals


def label(tenant: str, model: str, hour: datetime) -> str:
    return f"tenant={tenant} model={model} hour={hour.strftime('%Y-%m-%dT%H:%M:%SZ')}"


def self_check(events: dict, stored: dict) -> list[str]:
    """events and stored map (tenant, model, hour) to (tokens_in, tokens_out, event_count)."""
    drift = []
    for key in sorted(events.keys() | stored.keys()):
        if key not in stored:
            drift.append(f"DRIFT {label(*key)} events but no rollup row (events={events[key]})")
        elif key not in events:
            drift.append(f"DRIFT {label(*key)} rollup row but no events (rollup={stored[key]})")
        elif events[key] != stored[key]:
            names = ("tokens_in", "tokens_out", "event_count")
            parts = [f"{n} rollup={s} events={e}" for n, s, e in zip(names, stored[key], events[key]) if s != e]
            drift.append(f"DRIFT {label(*key)} " + "; ".join(parts))
    return drift


def against_actuals(actuals: dict, stored: dict, tolerance_bps: int) -> list[str]:
    """Drift when |rollup - actual| * 10000 > tolerance_bps * actual, per column; or a row on one side only."""
    drift = []
    for key in sorted(actuals.keys() | stored.keys()):
        if key not in stored:
            drift.append(f"DRIFT {label(*key)} actual but no rollup row (actual={actuals[key]})")
        elif key not in actuals:
            drift.append(f"DRIFT {label(*key)} rollup row but no actual (rollup={stored[key][:2]})")
        else:
            parts = []
            for name, rolled, actual in zip(("tokens_in", "tokens_out"), stored[key][:2], actuals[key]):
                if abs(rolled - actual) * 10000 > tolerance_bps * actual:
                    parts.append(f"{name} rollup={rolled} actual={actual} diff={rolled - actual}")
            if parts:
                drift.append(f"DRIFT {label(*key)} beyond {tolerance_bps} bps: " + "; ".join(parts))
    return drift


def rows_by_key(rows) -> dict:
    return {(r["tenant_id"], r["model"], r["hour_start"]): (r["tokens_in"], r["tokens_out"], r["event_count"]) for r in rows}


def run(conn: psycopg.Connection, since: datetime | None, only: datetime | None, actuals: dict | None, tolerance_bps: int):
    """Returns (hours checked, rows checked, drift lines, actuals skipped). Reads only."""
    hours = [r["hour_start"] for r in conn.execute(CLOSED_HOURS, {"since": since, "only": only}).fetchall()]
    events = rows_by_key(conn.execute(RECOMPUTE, {"hours": hours}).fetchall())
    stored = rows_by_key(conn.execute(STORED, {"hours": hours}).fetchall())
    conn.rollback()
    drift = self_check(events, stored)
    rows = len(events.keys() | stored.keys())
    skipped = 0
    if actuals is not None:
        in_scope = {k: v for k, v in actuals.items() if k[2] in hours}
        skipped = len(actuals) - len(in_scope)
        drift += against_actuals(in_scope, stored, tolerance_bps)
        rows += len(in_scope.keys() | stored.keys())
    return len(hours), rows, drift, skipped


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.reconcile", description="Read-only check of the hourly rollups against the raw events and, with --actuals, against another source.")
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--hour", metavar="ISO", help="check only this closed UTC hour, e.g. 2026-11-10T14:00:00Z")
    scope.add_argument("--since", metavar="ISO", help="check closed hours starting at or after this time")
    parser.add_argument("--actuals", metavar="FILE.jsonl", help="totals from another source, one JSON object per line")
    parser.add_argument("--tolerance-bps", type=int, default=100, metavar="BPS", help="allowed difference in basis points of the actual (default 100 = 1%%)")
    args = parser.parse_args(argv)  # argparse exits 2 on a bad argument
    try:
        if args.tolerance_bps < 0:
            raise BadInput("--tolerance-bps must be 0 or more")
        only = parse_hour(args.hour, "--hour") if args.hour else None
        since = parse_time(args.since, "--since") if args.since else None
        actuals = read_actuals(args.actuals) if args.actuals else None
    except BadInput as e:
        print(f"reconcile: {e}", file=sys.stderr)
        return 2

    conn = db.connect()
    try:
        conn.read_only = True
        print(f"reconcile: hour={args.hour or 'all closed'} since={args.since or '-'} actuals={args.actuals or '-'} tolerance_bps={args.tolerance_bps}", file=sys.stderr, flush=True)
        hours, rows, drift, skipped = run(conn, since, only, actuals, args.tolerance_bps)
        if only is not None and hours == 0:
            print(f"reconcile: --hour {args.hour} is not a closed hour", file=sys.stderr)
            return 2
    finally:
        conn.close()
    for line in drift:
        print(line)
    if skipped:
        print(f"reconcile: skipped {skipped} actuals rows whose hour is not a checked closed hour", file=sys.stderr)
    print(f"reconcile: hours={hours} rows={rows} drift={len(drift)}")
    return 1 if drift else 0


if __name__ == "__main__":
    sys.exit(main())
