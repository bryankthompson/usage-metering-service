"""Versioned price table and rating. Owner: dev B.

Money is an integer count of micro-USD (1 USD = 1,000,000). There is no float and no true division
anywhere in this module.

Prices are per model and versioned: a new price is a new row, a stored version is never changed
(a database trigger enforces it). `price_at` picks the version in force at a given instant, so a
charge for a past hour is rated with the price that applied then.
"""
import logging
import re
from bisect import bisect_right
from datetime import datetime, timedelta, timezone
from typing import Annotated

import psycopg
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BeforeValidator
from pydantic_core import PydanticCustomError

from app import db
from app.edge import require_entitlement

log = logging.getLogger(__name__)

router = APIRouter()


class NoPrice(Exception):
    """No price version is in force for the model at that time. Never rated as zero."""


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


def _require_int(value: object, name: str) -> None:
    # PostgreSQL would round a float (1.5 becomes 2) and take True as 1: a price is never silently changed.
    if type(value) is not int:
        raise TypeError(f"{name} must be an int of micro-USD, got {value!r}")


def add_price_version(conn: psycopg.Connection, model: str, effective_from: datetime,
                      input_micros_per_1k: int, output_micros_per_1k: int) -> int:
    """Store the next version (1, 2, ...) for `model` and return its number. Commits.

    Two writers racing for the same number collide on the primary key and one raises; neither
    overwrites the other. A price that is not an int (a float, a bool, a string) raises TypeError.
    """
    _require_aware(effective_from, "effective_from")
    _require_int(input_micros_per_1k, "input_micros_per_1k")
    _require_int(output_micros_per_1k, "output_micros_per_1k")
    row = conn.execute(
        "INSERT INTO price_versions (model, version, effective_from, input_micros_per_1k, output_micros_per_1k)"
        " SELECT %s, COALESCE(MAX(version), 0) + 1, %s, %s, %s FROM price_versions WHERE model = %s"
        " RETURNING version",
        (model, effective_from, input_micros_per_1k, output_micros_per_1k, model)).fetchone()
    conn.commit()
    log.info("price version added: model=%r version=%s effective_from=%s in=%s out=%s",
             model, row["version"], effective_from.isoformat(), input_micros_per_1k, output_micros_per_1k)
    return row["version"]


def price_at(conn: psycopg.Connection, model: str, at: datetime) -> dict:
    """The version with the greatest `effective_from <= at` (a tie goes to the later version).

    Raises NoPrice when the model has no version in force at `at`.
    """
    _require_aware(at, "at")
    row = conn.execute(
        "SELECT version, effective_from, input_micros_per_1k, output_micros_per_1k FROM price_versions"
        " WHERE model = %s AND effective_from <= %s ORDER BY effective_from DESC, version DESC LIMIT 1",
        (model, at)).fetchone()
    if row is None:
        raise NoPrice(f"no price for model {model!r} at {at.isoformat()}")
    return row


def _in_force(keys: list[datetime], versions: list[dict], at: datetime) -> dict | None:
    """What `price_at` selects, from versions already loaded and sorted by (effective_from, version); `keys` are their
    effective_from values. The last one at or before `at` wins, so a tie goes to the later version."""
    # Same-zone aware datetimes compare by wall clock, which is wrong across a DST fallback: compare in UTC.
    index = bisect_right(keys, at.astimezone(timezone.utc))
    return versions[index - 1] if index else None


def _rate(version: dict, tokens_in: int, tokens_out: int) -> int:
    if tokens_in < 0 or tokens_out < 0:
        raise ValueError("token counts cannot be negative")
    exact_thousandths = tokens_in * version["input_micros_per_1k"] + tokens_out * version["output_micros_per_1k"]
    return (exact_thousandths + 500) // 1000


def rate(conn: psycopg.Connection, model: str, tokens_in: int, tokens_out: int, at: datetime) -> int:
    """Cost in micro-USD of the tokens at the price in force at `at`.

    Rule: (tokens_in * input_price + tokens_out * output_price) is divided by 1,000 in integer
    arithmetic and rounded half up ONCE, on that sum (not on each side). 1 token at 1,500 micros per 1k
    is 1.5 micros, which rounds to 2.
    """
    return _rate(price_at(conn, model, at), tokens_in, tokens_out)


MAX_NAMED_MODELS = 20


def _unpriced_message(unpriced: dict[str, datetime]) -> str:
    """One model keeps the message `price_at` gives. Several are listed sorted, each with its first unpriced hour, at most
    MAX_NAMED_MODELS of them and then "and N more", so the operator can add every missing price in one pass."""
    names = sorted(unpriced)
    if len(names) == 1:
        return f"no price for model {names[0]!r} at {unpriced[names[0]].isoformat()}"
    listed = [f"{name!r} at {unpriced[name].isoformat()}" for name in names[:MAX_NAMED_MODELS]]
    if len(names) > MAX_NAMED_MODELS:
        listed.append(f"and {len(names) - MAX_NAMED_MODELS} more")
    return f"no price for {len(names)} models: " + ", ".join(listed)


def charge_lines(conn: psycopg.Connection, tenant_id: str, start: datetime, end: datetime) -> tuple[list[dict], int]:
    """Rate every hourly rollup row of the tenant with `start <= hour_start < end`, each at its own hour_start.

    Returns (lines, total_micros). Each line is rounded on its own (see `rate`) and the total is the
    sum of the lines. A whole hour is rated at the price in force at its start, so a version applies from the first
    hour that starts at or after its `effective_from`; usage inside an hour is never split between two prices.
    Raises NoPrice if any row has no price in force at its hour.
    """
    _require_aware(start, "start")
    _require_aware(end, "end")
    rows = conn.execute(
        "SELECT model, hour_start, tokens_in, tokens_out FROM usage_rollups_hourly"
        " WHERE tenant_id = %s AND hour_start >= %s AND hour_start < %s ORDER BY hour_start, model",
        (tenant_id, start, end)).fetchall()
    # One query for every version of the models in range, not one per row: a year of hourly rows is thousands.
    models = sorted({row["model"] for row in rows})
    loaded: dict[str, list[dict]] = {model: [] for model in models}
    if models:
        # A version effective after the last hour in range can never apply, so it is not loaded (nor decoded).
        for v in conn.execute(
                "SELECT model, version, effective_from, input_micros_per_1k, output_micros_per_1k FROM price_versions"
                " WHERE model = ANY(%s) AND effective_from <= %s ORDER BY effective_from, version",
                (models, rows[-1]["hour_start"])):
            loaded[v["model"]].append(v)
    keys = {model: [v["effective_from"].astimezone(timezone.utc) for v in versions] for model, versions in loaded.items()}
    lines = []
    unpriced: dict[str, datetime] = {}               # model -> the first hour in range with no price (rows are in hour order)
    for row in rows:
        version = _in_force(keys[row["model"]], loaded[row["model"]], row["hour_start"])
        if version is None:
            unpriced.setdefault(row["model"], row["hour_start"])     # keep going: name every unpriced model, not just the first
            continue
        lines.append({
            "model": row["model"],
            "hour_start": row["hour_start"],
            "tokens_in": row["tokens_in"],
            "tokens_out": row["tokens_out"],
            "price_version": version["version"],
            "micros": _rate(version, row["tokens_in"], row["tokens_out"]),
        })
    if unpriced:                                     # all or nothing: no partial bill
        raise NoPrice(_unpriced_message(unpriced))
    total = sum(line["micros"] for line in lines)
    log.info("charges computed: tenant=%r start=%s end=%s lines=%d total_micros=%d",
             tenant_id, start.isoformat(), end.isoformat(), len(lines), total)
    return lines, total


MAX_RANGE = timedelta(days=366)           # a wider range loads every hourly row at once: eight such calls took the API to 1.7 GiB
UNCLOSED_LIMIT = 100

# Hours that start inside the range and hold usage that is not late, but are not in closed_hours: usage the bill does not
# contain yet (the hour is still open, or the rollup is behind or stuck on it). Late events never close an hour, so not counted.
UNCLOSED_HOURS = """
SELECT hour_start FROM (
    SELECT DISTINCT date_trunc('hour', occurred_at, 'UTC') AS hour_start FROM usage_events
    WHERE tenant_id = %(tenant)s AND NOT late AND occurred_at >= %(start)s AND occurred_at < %(end)s + interval '1 hour'
) h
WHERE hour_start >= %(start)s AND hour_start < %(end)s
  AND NOT EXISTS (SELECT 1 FROM closed_hours c WHERE c.hour_start = h.hour_start)
ORDER BY hour_start LIMIT %(limit)s
"""


def unclosed_hours(conn: psycopg.Connection, tenant_id: str, start: datetime, end: datetime) -> list[datetime]:
    """The earliest UNCLOSED_LIMIT hours in [start, end) that hold usage the charges do not include yet."""
    rows = conn.execute(UNCLOSED_HOURS, {"tenant": tenant_id, "start": start, "end": end, "limit": UNCLOSED_LIMIT})
    return [r["hour_start"] for r in rows.fetchall()]


ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _iso_text(value: object) -> object:
    """Only ISO 8601 text: a bare number is read as Unix seconds by the datetime parser (`?start=5` is 1970), and a
    caller who sends one almost certainly meant something else. Every spelling that starts YYYY-MM-DD passes unchanged."""
    if not isinstance(value, str) or not ISO_DATE.match(value):
        raise PydanticCustomError("iso_datetime", "must be ISO 8601 text with a time zone, such as 2026-11-01T00:00:00Z")
    return value


IsoDatetime = Annotated[datetime, BeforeValidator(_iso_text)]


@router.get("/v1/tenants/{tenant_id}/charges")
def get_charges(tenant_id: str, start: IsoDatetime, end: IsoDatetime,
                caller: str = Depends(require_entitlement)) -> dict:
    if caller != tenant_id:
        raise HTTPException(status_code=403, detail="tenant does not match the caller")
    for name, value in (("start", start), ("end", end)):
        if value.tzinfo is None:
            raise HTTPException(status_code=422, detail=f"{name} must be timezone-aware")
    if start >= end:
        raise HTTPException(status_code=422, detail="start must be before end")
    if end - start > MAX_RANGE:
        raise HTTPException(status_code=422, detail=f"the range is wider than {MAX_RANGE.days} days: ask for the period in pieces")
    # Opened here, not in a dependency (as in `ingest_usage`): a dependency's connection stays open while the request waits
    # for a free worker thread, and a refused request above never needs one. A database error reaches the handler in main.py.
    with db.connect() as conn:
        # Read before the rows: an hour that closes in between is then listed AND billed (the safe direction), never neither.
        unclosed = unclosed_hours(conn, tenant_id, start, end)
        try:
            lines, total = charge_lines(conn, tenant_id, start, end)
        except NoPrice as err:
            log.error("charges refused: %s", err)
            raise HTTPException(status_code=409, detail=str(err)) from err
    if unclosed:
        log.info("charges partial: tenant=%r %d hour(s) with usage are not closed yet, first %s", tenant_id, len(unclosed),
                 unclosed[0].isoformat())
    return {"tenant_id": tenant_id, "start": start, "end": end, "lines": lines, "total_micros": total,
            "unclosed_hours": unclosed}
