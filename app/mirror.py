"""A derived copy of the closed hourly rollups in ClickHouse, over its HTTP interface (urllib, no new dependency).

The Postgres rollup stays the bill and the append-only log stays the source. `rollup.close_hour` calls `mirror_rows`
after its commit when `CLICKHOUSE_URL` is set; a failure there is logged and never touches the close. `--backfill`
copies every closed hour that is missing or different in ClickHouse; `--export-actuals FILE` writes ClickHouse's totals as
the JSON lines file `python -m app.reconcile --actuals` reads. See docs/clickhouse.md.

CLI: `python -m app.mirror --backfill | --export-actuals FILE`.
"""
import argparse
import json
import logging
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from app import db

log = logging.getLogger(__name__)

TABLE = "usage_rollups_hourly"
TIMEOUT_S = 5

# ReplacingMergeTree keeps one row per sorting key (tenant, model, hour) after a background merge, the one with the
# highest `version`. A repeated write of an hour is therefore a replacement, not a second count. Merges are lazy, so
# every read here uses FINAL, which collapses duplicates at query time.
CREATE_TABLE = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    tenant_id   String,
    model       String,
    hour_start  DateTime('UTC'),
    tokens_in   Int64,
    tokens_out  Int64,
    event_count Int64,
    version     UInt64
) ENGINE = ReplacingMergeTree(version)
ORDER BY (tenant_id, model, hour_start)
"""

CLOSED_ROLLUPS = """
SELECT r.tenant_id, r.model, r.hour_start, r.tokens_in, r.tokens_out, r.event_count
FROM usage_rollups_hourly r
WHERE r.hour_start IN (SELECT hour_start FROM closed_hours)
ORDER BY r.hour_start, r.tenant_id, r.model
"""


def clickhouse_url() -> str | None:
    """CLICKHOUSE_URL, or None when it is unset or empty (the mirror is off)."""
    return os.environ.get("CLICKHOUSE_URL") or None


def _post(url: str, query: str, body: bytes = b"") -> bytes:
    """One HTTP POST to ClickHouse. Raises on a refused connection, a timeout or a non-2xx answer (with its text)."""
    request = urllib.request.Request(url.rstrip("/") + "/?" + urllib.parse.urlencode({"query": query}), data=body, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            return response.read()
    except urllib.error.HTTPError as err:
        raise RuntimeError(f"clickhouse answered {err.code}: {err.read(500).decode(errors='replace').strip()}") from err


def _utc(hour: datetime) -> datetime:
    return hour.astimezone(timezone.utc)


def _row_key(row: dict) -> tuple:
    return (row["tenant_id"], row["model"], _utc(row["hour_start"]))


def _row_values(row: dict) -> tuple[int, int, int]:
    return (row["tokens_in"], row["tokens_out"], row["event_count"])


def write_rows(url: str, rows: list[dict]) -> None:
    """Create the table if needed and insert `rows` (Postgres rollup rows). Raises on any failure."""
    if not rows:
        return
    version = int(datetime.now(timezone.utc).timestamp() * 1_000_000)  # a later write of a row wins
    lines = [
        json.dumps({
            "tenant_id": r["tenant_id"], "model": r["model"], "hour_start": _utc(r["hour_start"]).strftime("%Y-%m-%d %H:%M:%S"),
            "tokens_in": r["tokens_in"], "tokens_out": r["tokens_out"], "event_count": r["event_count"], "version": version,
        })
        for r in rows
    ]
    _post(url, CREATE_TABLE)
    _post(url, f"INSERT INTO {TABLE} FORMAT JSONEachRow", "\n".join(lines).encode())


def mirror_rows(hour: datetime, rows: list[dict]) -> bool:
    """Best effort, called after the Postgres commit. Returns True if the hour is mirrored or the mirror is off.

    Never raises: a failure is logged as `clickhouse mirror failed: hour=...` and `--backfill` recovers it.
    """
    url = clickhouse_url()
    if url is None:
        return True
    try:
        write_rows(url, rows)
    except Exception as err:
        log.error("clickhouse mirror failed: hour=%s error=%r", _utc(hour).isoformat(), err)
        return False
    return True


def read_mirror(url: str) -> dict[tuple, tuple[int, int, int]]:
    """Every (tenant, model, hour) in ClickHouse with its (tokens_in, tokens_out, event_count), duplicates collapsed."""
    query = (f"SELECT tenant_id, model, toUnixTimestamp(hour_start) AS ts, tokens_in, tokens_out, event_count "
             f"FROM {TABLE} FINAL FORMAT JSONEachRow")
    out = {}
    _post(url, CREATE_TABLE)
    for line in _post(url, query).splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        hour = datetime.fromtimestamp(int(r["ts"]), timezone.utc)
        out[(r["tenant_id"], r["model"], hour)] = (int(r["tokens_in"]), int(r["tokens_out"]), int(r["event_count"]))
    return out


def backfill(conn, url: str) -> tuple[int, list[datetime]]:
    """Send every closed hour whose rows are missing or different in ClickHouse. Returns (hours sent, hours that failed).

    Rows that exist only in ClickHouse are not looked at here (nothing in Postgres says what to do with them); the
    `--export-actuals` plus `app.reconcile --actuals` check reports them.
    """
    rows = conn.execute(CLOSED_ROLLUPS).fetchall()
    conn.commit()
    mirrored = read_mirror(url)
    by_hour: dict[datetime, list[dict]] = {}
    for row in rows:
        if mirrored.get(_row_key(row)) != _row_values(row):
            by_hour.setdefault(_utc(row["hour_start"]), []).append(row)
    sent, failed = 0, []
    for hour, hour_rows in sorted(by_hour.items()):
        try:
            write_rows(url, [r for r in rows if _utc(r["hour_start"]) == hour])  # the whole hour, so a half-written hour is whole
        except Exception as err:
            log.error("clickhouse mirror failed: hour=%s error=%r", hour.isoformat(), err)
            failed.append(hour)
        else:
            sent += 1
    return sent, failed


def export_actuals(url: str, path: str) -> int:
    """Write ClickHouse's totals as JSON lines for `app.reconcile --actuals`. Returns the number of lines."""
    mirrored = read_mirror(url)
    with open(path, "w") as f:
        for (tenant, model, hour), (tokens_in, tokens_out, _events) in sorted(mirrored.items()):
            f.write(json.dumps({"tenant_id": tenant, "model": model, "hour": hour.isoformat(),
                                "tokens_in": tokens_in, "tokens_out": tokens_out}) + "\n")
    return len(mirrored)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m app.mirror", description="Recover or check the ClickHouse copy of the closed rollups.")
    what = parser.add_mutually_exclusive_group(required=True)
    what.add_argument("--backfill", action="store_true", help="copy every closed hour that is missing or different in ClickHouse")
    what.add_argument("--export-actuals", metavar="FILE", help="write ClickHouse's totals as JSON lines for app.reconcile --actuals")
    args = parser.parse_args(argv)
    url = clickhouse_url()
    if url is None:
        parser.error("CLICKHOUSE_URL is not set")
    try:
        if args.backfill:
            conn = db.connect(statement_timeout_ms=0)
            try:
                sent, failed = backfill(conn, url)
            finally:
                conn.close()
            print(f"mirror: sent {sent} hours, failed {len(failed)}", flush=True)
            if failed:
                sys.exit(1)
        else:
            print(f"mirror: wrote {export_actuals(url, args.export_actuals)} rows to {args.export_actuals}", flush=True)
    except (RuntimeError, OSError) as err:  # clickhouse unreachable or refused: say so, non-zero
        print(f"mirror: {err}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    main()
