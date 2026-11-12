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
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

import psycopg
from psycopg.rows import DictRow

from app import db, mirror

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
RETURNING tenant_id, model, hour_start, tokens_in, tokens_out, event_count
"""

# Hours that have events, are not closed, and ended at least `grace` before now (oldest first).
# Walks the hours that have events with the (occurred_at) index: from one hour, the first event at or after the next hour
# boundary names the next hour. That is one index probe per hour with events, not one row per event, and closed hours
# are still visited (unclosed hours can be anywhere in the past), so the cost grows with hours, not with events.
# `occurred_at > h.hour_start` makes every step move forward, so the walk ends even at an 'infinity' or '-infinity' row
# (the schema allows them, the API does not): without it those rows find themselves again and the query never ends.
DUE_HOURS = """
WITH RECURSIVE hours(hour_start) AS (
    SELECT date_trunc('hour', first.occurred_at, 'UTC')
    FROM (SELECT occurred_at FROM usage_events ORDER BY occurred_at LIMIT 1) first
  UNION ALL
    SELECT date_trunc('hour', next.occurred_at, 'UTC')
    FROM hours h
    CROSS JOIN LATERAL (
        SELECT occurred_at FROM usage_events
        WHERE occurred_at >= h.hour_start + interval '1 hour' AND occurred_at > h.hour_start
        ORDER BY occurred_at LIMIT 1
    ) next
)
SELECT h.hour_start
FROM hours h
WHERE h.hour_start + interval '1 hour' <= %(cutoff)s
  AND NOT EXISTS (SELECT 1 FROM closed_hours c WHERE c.hour_start = h.hour_start)
ORDER BY h.hour_start
"""


def hour_index(ts: datetime) -> int:
    """Whole hours since the epoch (UTC) of the hour that contains `ts`: the advisory lock's second key."""
    return (ts - EPOCH) // HOUR


def lock_hour_shared(conn: psycopg.Connection[DictRow], ts: datetime) -> None:
    """Ingest takes this before its INSERT. Many ingests share it; `close_hour` waits for all of them."""
    conn.execute("SELECT pg_advisory_xact_lock_shared(%s, %s)", (HOUR_LOCK_CLASS, hour_index(ts)))


def _require_aware(ts: datetime, name: str) -> None:
    if ts.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware, got {ts!r}")


def close_hour(
    conn: psycopg.Connection[DictRow], hour_start: datetime, defer_mirror: list | None = None
) -> tuple[int, int] | None:
    """Close one UTC hour. Returns (rollup rows written, events counted), or None if it was already closed.

    Commits (or rolls back and re-raises). Closing an hour twice changes nothing. After the commit, when
    CLICKHOUSE_URL is set, the rows are copied to ClickHouse; that copy can fail without failing the close (app/mirror.py).
    With `defer_mirror` (a list), the copy is not made here: (hour, rows) is appended to it for the caller to mirror. Nothing is appended when the mirror is off.
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
    if defer_mirror is not None and mirror.clickhouse_url():  # mirror off: nothing to mirror, so hold nothing
        defer_mirror.append((hour, written))
    else:
        mirror.mirror_rows(hour, written)  # best effort, after the commit: never raises, never touches the close
    return len(written), sum(r["event_count"] for r in written)


def close_due_hours(
    conn: psycopg.Connection[DictRow],
    now: datetime,
    grace: timedelta = DEFAULT_GRACE,
    failed: list[datetime] | None = None,
    reconnect: Callable[[], psycopg.Connection[DictRow]] | None = None,
) -> list[tuple[datetime, int, int]]:
    """Close every unclosed hour with events that ended at least `grace` before `now`, oldest first.

    Returns (hour_start, rows written, events counted) for each hour this call closed. One hour that fails (for
    example a BIGINT overflow) is rolled back, logged at ERROR with its hour, appended to `failed` and skipped, so
    it cannot block the hours after it; the next run retries it. Hours may close out of order: an unclosed earlier
    hour does not make a later event late, because lateness is judged per hour.

    A failure that lost the connection (the server restarted or killed it) is that hour's failure like any other, but
    the dead connection would fail every hour after it. With `reconnect`, each hour that finds the connection broken
    asks it for a new one first; if that raises, that hour fails like any other (logged, in `failed`) and the next hour
    asks again. The caller still owns `conn` and closes it (closing a dead one is harmless); this function closes the
    connections it got from `reconnect`. The failed hour is not retried here.

    The ClickHouse copy is off this loop's path: every due hour is closed first, then the closed hours are mirrored in
    one pass (`mirror.mirror_hours`), which stops at the first failure that means ClickHouse is down. The mirror never
    changes what this returns or what is in `failed`.
    """
    _require_aware(now, "now")
    hours = [r["hour_start"] for r in conn.execute(DUE_HOURS, {"cutoff": now - grace}).fetchall()]
    conn.commit()
    closed = []
    to_mirror: list[tuple[datetime, list[dict]]] = []  # (hour, rows) of the hours closed here, mirrored after the loop
    opened = None  # the connection last obtained from `reconnect`, which this function must close

    def fail(hour: datetime, err: Exception) -> None:
        log.error("rollup failed: hour=%s error=%r", hour.astimezone(timezone.utc).isoformat(), err)
        if failed is not None:
            failed.append(hour)

    try:
        for hour in hours:
            if conn.broken and reconnect is not None:  # lost, not merely rolled back: this hour needs a live one
                try:
                    fresh = reconnect()
                except Exception as err:  # still down: this hour fails, the next one tries again
                    fail(hour, err)
                    continue
                if opened is not None:
                    opened.close()
                conn = opened = fresh
            try:
                result = close_hour(conn, hour, defer_mirror=to_mirror)
            except Exception as err:  # close_hour has rolled back: nothing of this hour is written
                fail(hour, err)
                continue
            if result is not None:  # None: another process closed it first
                closed.append((hour, *result))
    finally:
        if opened is not None:
            opened.close()
    mirror.mirror_hours(to_mirror)
    return closed


def _connect() -> psycopg.Connection[DictRow]:
    return db.connect(statement_timeout_ms=0)  # no statement timeout: a large hour legitimately takes long


def run_once() -> list[datetime]:
    """Close every due hour once and print the result. Returns the hours that failed."""
    failed: list[datetime] = []
    conn = _connect()
    try:
        closed = close_due_hours(conn, datetime.now(timezone.utc), failed=failed, reconnect=_connect)
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
    parser = argparse.ArgumentParser(
        prog="python -m app.rollup", description="Close every due hour once, or keep doing it with --every."
    )
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
