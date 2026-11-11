"""Hourly rollups. Owner: dev A.

`close_hour` aggregates one UTC hour of usage events per (tenant, model) into `usage_rollups_hourly`
and records the hour in `closed_hours`, in one transaction. Every event of a closed hour is in its
closed rollup or flagged `late`, never both and never neither, because ingest and close serialize on a
per-hour advisory lock (see docs/ingest.md).

CLI: `python -m app.rollup` closes every due hour once; `--every SECONDS` repeats forever.
"""
import argparse
import logging
import sys
import time
from datetime import datetime, timedelta, timezone

import psycopg

from app import db

log = logging.getLogger(__name__)

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
HOUR = timedelta(hours=1)
DEFAULT_GRACE = timedelta(minutes=5)
HOUR_LOCK_CLASS = 1  # first key of the two-key advisory lock; the second is the hour index

CLOSE_HOUR = "INSERT INTO closed_hours (hour_start) VALUES (%s) ON CONFLICT DO NOTHING RETURNING hour_start"

# Only events of this UTC hour that are not late. Late events never enter a closed rollup.
AGGREGATE_HOUR = """
INSERT INTO usage_rollups_hourly (tenant_id, model, hour_start, tokens_in, tokens_out, event_count)
SELECT tenant_id, model, %(hour)s, sum(tokens_in), sum(tokens_out), count(*)
FROM usage_events
WHERE occurred_at >= %(hour)s AND occurred_at < %(hour)s + interval '1 hour' AND NOT late
GROUP BY tenant_id, model
RETURNING event_count
"""

# Hours that have events, are not closed, and ended at least `grace` before now (oldest first).
DUE_HOURS = """
SELECT DISTINCT date_trunc('hour', e.occurred_at, 'UTC') AS hour_start
FROM usage_events e
WHERE date_trunc('hour', e.occurred_at, 'UTC') + interval '1 hour' <= %(cutoff)s
  AND NOT EXISTS (SELECT 1 FROM closed_hours c WHERE c.hour_start = date_trunc('hour', e.occurred_at, 'UTC'))
ORDER BY hour_start
"""


def hour_index(ts: datetime) -> int:
    """Whole hours since the epoch (UTC) of the hour that contains `ts`: the advisory lock's second key."""
    return (ts - EPOCH) // HOUR


def lock_hour_shared(conn: psycopg.Connection, ts: datetime) -> None:
    """Ingest takes this before its INSERT. Many ingests share it; `close_hour` waits for all of them."""
    conn.execute("SELECT pg_advisory_xact_lock_shared(%s, %s)", (HOUR_LOCK_CLASS, hour_index(ts)))


def _require_aware(ts: datetime, name: str) -> None:
    if ts.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware, got {ts!r}")


def close_hour(conn: psycopg.Connection, hour_start: datetime) -> tuple[int, int] | None:
    """Close one UTC hour. Returns (rollup rows written, events counted), or None if it was already closed.

    Commits (or rolls back and re-raises). Closing an hour twice changes nothing.
    """
    _require_aware(hour_start, "hour_start")
    hour = hour_start.astimezone(timezone.utc)
    if hour != hour.replace(minute=0, second=0, microsecond=0):
        raise ValueError(f"hour_start must be on a UTC hour boundary, got {hour_start!r}")
    try:
        # Exclusive: waits for ingests of this hour already in flight (they hold the shared lock until
        # they commit), and makes later ones wait until this commit, when they see the hour as closed
        # and store the event with late = true. Each statement below runs after the lock is held, so its
        # snapshot includes every ingest that got in first.
        conn.execute("SELECT pg_advisory_xact_lock(%s, %s)", (HOUR_LOCK_CLASS, hour_index(hour)))
        if conn.execute(CLOSE_HOUR, (hour,)).fetchone() is None:
            conn.commit()
            return None
        written = conn.execute(AGGREGATE_HOUR, {"hour": hour}).fetchall()
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return len(written), sum(r["event_count"] for r in written)


def close_due_hours(
    conn: psycopg.Connection, now: datetime, grace: timedelta = DEFAULT_GRACE, failed: list[datetime] | None = None
) -> list[tuple[datetime, int, int]]:
    """Close every unclosed hour with events that ended at least `grace` before `now`, oldest first.

    Returns (hour_start, rows written, events counted) for each hour this call closed. One hour that fails (for
    example a BIGINT overflow) is rolled back, logged at ERROR with its hour, appended to `failed` and skipped, so
    it cannot block the hours after it; the next run retries it. Hours may close out of order: an unclosed earlier
    hour does not make a later event late, because lateness is judged per hour.
    """
    _require_aware(now, "now")
    hours = [r["hour_start"] for r in conn.execute(DUE_HOURS, {"cutoff": now - grace}).fetchall()]
    conn.commit()
    closed = []
    for hour in hours:
        try:
            result = close_hour(conn, hour)
        except Exception as err:  # close_hour has rolled back: nothing of this hour is written
            log.error("rollup failed: hour=%s error=%r", hour.astimezone(timezone.utc).isoformat(), err)
            if failed is not None:
                failed.append(hour)
            continue
        if result is not None:  # None: another process closed it first
            closed.append((hour, *result))
    return closed


def run_once() -> list[datetime]:
    """Close every due hour once and print the result. Returns the hours that failed."""
    failed: list[datetime] = []
    conn = db.connect(statement_timeout_ms=0)  # no statement timeout: a large hour legitimately takes long
    try:
        closed = close_due_hours(conn, datetime.now(timezone.utc), failed=failed)
    finally:
        conn.close()
    for hour, rows, events in closed:
        print(f"closed hour={hour.astimezone(timezone.utc).isoformat()} rows={rows} events={events}", flush=True)
    if failed:
        hours = ", ".join(h.astimezone(timezone.utc).isoformat() for h in failed)
        print(f"rollup: closed {len(closed)} hours, failed {len(failed)}: {hours}", flush=True)
    elif not closed:
        print("nothing due", flush=True)
    return failed


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m app.rollup", description="Close every due hour once, or keep doing it with --every.")
    parser.add_argument("--every", type=int, metavar="SECONDS", help="repeat forever, sleeping SECONDS between runs")
    args = parser.parse_args(argv)
    if args.every is not None and args.every <= 0:
        parser.error("--every must be a positive number of seconds")
    failed = run_once()
    while args.every:  # a failed hour is logged every cycle and never stops the loop
        time.sleep(args.every)
        failed = run_once()
    if failed:
        sys.exit(1)  # one-shot run: a cron or the restart policy must see that an hour failed


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    main()
