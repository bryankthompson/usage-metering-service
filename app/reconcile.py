"""Reconcile: a read-only check that the stored hourly rollups can be trusted.

Self-check (default): for every closed hour, recompute the per-(tenant, model) sums from `usage_events`
(`NOT late`, UTC hour of `occurred_at`) and compare them with `usage_rollups_hourly`. The recompute below is its
own SQL on purpose and does not import `rollup.AGGREGATE_HOUR`: two implementations make a bug in either show as drift.

Also drift: a rollup row whose hour `closed_hours` does not list, because the bill (`pricing.charge_lines`) reads every rollup row.

`--actuals FILE.jsonl`: also compare the rollups with totals reported by another source, within `--tolerance-bps`.

"Not billed" (always printed, information only): the count and token sums of late events, of events whose hour ended over
2 hours ago and is still not closed, and of events dated over 1 hour ahead. `--fail-on-unbilled` makes any of them exit 1.

CLI: `python -m app.reconcile`. Exit 0 clean, 1 drift found, 2 bad argument or malformed actuals line.
Integer arithmetic only. Details in docs/reconcile.md.
"""

import argparse
import json
import sys
from datetime import datetime, timezone

import psycopg
from psycopg.rows import DictRow

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

# A rollup row whose hour is not in closed_hours. Charges bills it, and the two queries above never read it.
ORPHAN_ROWS = """
SELECT tenant_id, model, hour_start, tokens_in, tokens_out, event_count FROM usage_rollups_hourly r
WHERE NOT EXISTS (SELECT 1 FROM closed_hours c WHERE c.hour_start = r.hour_start)
  AND (%(since)s::timestamptz IS NULL OR hour_start >= %(since)s) AND (%(only)s::timestamptz IS NULL OR hour_start = %(only)s)
ORDER BY hour_start, tenant_id, model
"""

# Events no rollup will bill. late = its hour was closed on arrival. unclosed = the hour ended over 2 hours ago (hour_start
# before now - 3 h) and is still not closed. future = dated over 1 hour ahead (an hour that far ahead cannot be closed yet).
# Sums stay numeric: past BIGINT is still exact. The overdue test compares hour_start, so an event in year 9999 cannot overflow.
NOT_BILLED = """
SELECT kind, count(*) AS events, sum(tokens_in) AS tokens_in, sum(tokens_out) AS tokens_out
FROM (SELECT CASE WHEN late THEN 'late'
                  WHEN occurred_at > %(now)s::timestamptz + interval '1 hour' THEN 'future'
                  WHEN date_trunc('hour', occurred_at, 'UTC') < %(now)s::timestamptz - interval '3 hours'
                       AND NOT EXISTS (SELECT 1 FROM closed_hours c WHERE c.hour_start = date_trunc('hour', occurred_at, 'UTC'))
                       THEN 'unclosed' END AS kind, tokens_in, tokens_out
      FROM usage_events
      WHERE (%(since)s::timestamptz IS NULL OR date_trunc('hour', occurred_at, 'UTC') >= %(since)s)
        AND (%(only)s::timestamptz IS NULL OR date_trunc('hour', occurred_at, 'UTC') = %(only)s)) e
WHERE kind IS NOT NULL GROUP BY kind
"""

NOT_BILLED_KINDS = (
    ("late", "its hour was already closed when it arrived"),
    ("unclosed", "its hour ended over 2 hours ago and is still not closed"),
    ("future", "dated over 1 hour ahead"),
)

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
    except UnicodeDecodeError:
        raise BadInput(f"{path}: not valid UTF-8") from None
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
        actual_key = (row["tenant_id"], row["model"], parse_hour(row["hour"], f"{where}: hour"))
        if actual_key in actuals:
            raise BadInput(f"{where}: repeats tenant_id, model and hour of an earlier line")
        actuals[actual_key] = (row["tokens_in"], row["tokens_out"])
    return actuals


def _plain(name: str) -> str:
    """A name as it is, unless it holds a newline, an escape or another unprintable character: then repr(), so a
    caller-chosen tenant or model name cannot start a line of its own in the report."""
    return name if name.isprintable() else repr(name)


def label(tenant: str, model: str, hour: datetime) -> str:
    return f"tenant={_plain(tenant)} model={_plain(model)} hour={hour.strftime('%Y-%m-%dT%H:%M:%SZ')}"


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
    return {
        (r["tenant_id"], r["model"], r["hour_start"]): (r["tokens_in"], r["tokens_out"], r["event_count"]) for r in rows
    }


def orphan_drift(rows) -> list[str]:
    return [
        f"DRIFT {label(r['tenant_id'], r['model'], r['hour_start'])} rollup row but its hour is not in closed_hours"
        f" (rollup={(r['tokens_in'], r['tokens_out'], r['event_count'])})"
        for r in rows
    ]


def not_billed_lines(not_billed: dict) -> list[str]:
    return [
        f"not billed: {kind} events={n['events']} tokens_in={n['tokens_in']} tokens_out={n['tokens_out']} ({why})"
        for (kind, why), n in ((k, not_billed[k[0]]) for k in NOT_BILLED_KINDS)
    ]


def run(
    conn: psycopg.Connection[DictRow],
    since: datetime | None,
    only: datetime | None,
    actuals: dict | None,
    tolerance_bps: int,
    now: datetime | None = None,
):
    """Returns (hours checked, rows checked, drift lines, actuals skipped, not billed). Reads only. `now` is for tests."""
    scope = {"since": since, "only": only}
    hours = [r["hour_start"] for r in conn.execute(CLOSED_HOURS, scope).fetchall()]
    events = rows_by_key(conn.execute(RECOMPUTE, {"hours": hours}).fetchall())
    stored = rows_by_key(conn.execute(STORED, {"hours": hours}).fetchall())
    orphans = conn.execute(ORPHAN_ROWS, scope).fetchall()
    found = {
        r["kind"]: {"events": r["events"], "tokens_in": int(r["tokens_in"]), "tokens_out": int(r["tokens_out"])}
        for r in conn.execute(NOT_BILLED, {**scope, "now": now or datetime.now(timezone.utc)}).fetchall()
    }
    not_billed = {kind: found.get(kind, {"events": 0, "tokens_in": 0, "tokens_out": 0}) for kind, _ in NOT_BILLED_KINDS}
    conn.rollback()
    drift = self_check(events, stored) + orphan_drift(orphans)
    rows = len(events.keys() | stored.keys()) + len(orphans)
    skipped = 0
    if actuals is not None:
        in_scope = {k: v for k, v in actuals.items() if k[2] in hours}
        skipped = len(actuals) - len(in_scope)
        drift += against_actuals(in_scope, stored, tolerance_bps)
        rows += len(in_scope.keys() | stored.keys())
    return len(hours), rows, drift, skipped, not_billed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.reconcile",
        description="Read-only check of the hourly rollups against the raw events and, with --actuals, against another source.",
    )
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--hour", metavar="ISO", help="check only this closed UTC hour, e.g. 2026-11-10T14:00:00Z")
    scope.add_argument("--since", metavar="ISO", help="check closed hours starting at or after this time")
    parser.add_argument("--actuals", metavar="FILE.jsonl", help="totals from another source, one JSON object per line")
    parser.add_argument(
        "--tolerance-bps",
        type=int,
        default=100,
        metavar="BPS",
        help="allowed difference in basis points of the actual (default 100 = 1%%)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print one JSON object (hours, rows, drift, not_billed) instead of text lines",
    )
    parser.add_argument(
        "--fail-on-unbilled",
        action="store_true",
        help="exit 1 when any late, unclosed or far-ahead event exists (default: information only)",
    )
    args = parser.parse_args(argv)  # argparse exits 2 on a bad argument
    try:
        if args.tolerance_bps < 0:
            raise BadInput("--tolerance-bps must be 0 or more")
        only = parse_hour(args.hour, "--hour") if args.hour is not None else None
        since = parse_time(args.since, "--since") if args.since is not None else None
        actuals = read_actuals(args.actuals) if args.actuals is not None else None
    except BadInput as e:
        print(f"reconcile: {e}", file=sys.stderr)
        return 2

    conn = db.connect(statement_timeout_ms=0)
    try:
        conn.read_only = True
        print(
            f"reconcile: hour={args.hour or 'all closed'} since={args.since or '-'} actuals={args.actuals or '-'} tolerance_bps={args.tolerance_bps}",
            file=sys.stderr,
            flush=True,
        )
        hours, rows, drift, skipped, not_billed = run(conn, since, only, actuals, args.tolerance_bps)
        if only is not None and hours == 0:
            print(f"reconcile: --hour {args.hour} is not a closed hour", file=sys.stderr)
            return 2
    finally:
        conn.close()
    if skipped:
        print(f"reconcile: skipped {skipped} actuals rows whose hour is not a checked closed hour", file=sys.stderr)
    if args.json:
        print(json.dumps({"hours": hours, "rows": rows, "drift": drift, "not_billed": not_billed}))
    else:
        for line in drift + not_billed_lines(not_billed):
            print(line)
        print(f"reconcile: hours={hours} rows={rows} drift={len(drift)}")
    unbilled = args.fail_on_unbilled and any(n["events"] for n in not_billed.values())
    return 1 if drift or unbilled else 0


if __name__ == "__main__":
    sys.exit(main())
