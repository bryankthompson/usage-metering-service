"""Versioned price table and rating. Owner: dev B.

Money is an integer count of micro-USD (1 USD = 1,000,000). There is no float and no true division
anywhere in this module.

Prices are per model and versioned: a new price is a new row, a stored version is never changed
(a database trigger enforces it). `price_at` picks the version in force at a given instant, so a
charge for a past hour is rated with the price that applied then.
"""
import base64
import json
import logging
import re
import time
from bisect import bisect_right
from datetime import datetime, timedelta, timezone
from typing import Annotated, NamedTuple

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BeforeValidator
from pydantic_core import PydanticCustomError

from app import db
from app.edge import require_entitlement
from app.ingest import MAX_TEXT

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


class TooManyLines(Exception):
    """The range holds more lines than the caller asked to have in one body."""


class RangeChanged(Exception):
    """The lines before a cursor are no longer the ones the cursor was made from."""


class ForeignCursor(Exception):
    """The cursor was made for another tenant or range."""


class Cursor(NamedTuple):
    """Where a page stopped: the last line it held, and what the lines up to and including it counted and cost then."""
    hour_start: datetime
    model: str
    count_before: int
    micros_before: int


class Page(NamedTuple):
    lines: list[dict]
    total_micros: int           # the whole range, whatever the page
    line_count: int             # the whole range
    next_cursor: Cursor | None


LINE_CAP = 10_000                # most lines in one body; a range with more is paged
MAX_PAGE = LINE_CAP
FETCH_ROWS = 1000                # rows the database sends per round trip of the pass

# `past` is decided by the database, with the column's collation: the same order as ORDER BY. Comparing `model` in Python would
# order "Zebra" before "apple" where the database orders it after, and a page boundary would skip or repeat a line.
CHARGE_ROWS = """
SELECT model, hour_start, tokens_in, tokens_out,
       COALESCE((hour_start, model) > (%(after_hour)s::timestamptz, %(after_model)s::text), TRUE) AS past
FROM usage_rollups_hourly WHERE tenant_id = %(tenant)s AND hour_start >= %(start)s AND hour_start < %(end)s
ORDER BY hour_start, model
"""


def _charge_pass(conn: psycopg.Connection, tenant_id: str, start: datetime, end: datetime, *, after: Cursor | None,
                 limit: int | None, max_rows: int | None) -> Page:
    """One pass over every rollup row of the range, in constant memory (a server-side cursor), keeping only the page.

    Every row is rated, so the total, the line count and the unpriced check cover the whole range whatever the page. The rows
    up to `after` are counted and summed but not kept; they must equal what `after` says (RangeChanged otherwise). Raises
    NoPrice if any row has no price, TooManyLines if the range has more than `max_rows` rows, and QueryCanceled when the pass
    runs past the request's statement timeout (that timeout is per statement, so a loop of fetches would escape it).
    """
    _require_aware(start, "start")
    _require_aware(end, "end")
    deadline = time.monotonic() + db.REQUEST_STATEMENT_TIMEOUT_MS / 1000 if db.REQUEST_STATEMENT_TIMEOUT_MS else None
    loaded: dict[str, list[dict]] = {}              # model -> its versions, read the first time the model shows up
    keys: dict[str, list[datetime]] = {}
    unpriced: dict[str, datetime] = {}              # model -> the first hour in range with no price (rows are in hour order)
    page: list[dict] = []
    before_count = before_micros = line_count = total = 0
    last: tuple[datetime, str] | None = None        # the last line kept on the page
    params = {"tenant": tenant_id, "start": start, "end": end,
              "after_hour": after.hour_start if after else None, "after_model": after.model if after else None}
    with conn.cursor(name="charge_rows") as cursor:
        cursor.itersize = FETCH_ROWS
        cursor.execute(CHARGE_ROWS, params)
        seen = 0
        for row in cursor:
            seen += 1
            if max_rows is not None and seen > max_rows:
                raise TooManyLines(f"the range holds more than {max_rows} lines")
            if deadline is not None and time.monotonic() > deadline:
                raise psycopg.errors.QueryCanceled("the charges pass ran past the statement timeout")
            model = row["model"]
            if model not in loaded:
                # A version effective at or after `end` can never apply to an hour in range, so it is not loaded.
                loaded[model] = conn.execute(
                    "SELECT model, version, effective_from, input_micros_per_1k, output_micros_per_1k FROM price_versions"
                    " WHERE model = %s AND effective_from < %s ORDER BY effective_from, version", (model, end)).fetchall()
                keys[model] = [v["effective_from"].astimezone(timezone.utc) for v in loaded[model]]
            version = _in_force(keys[model], loaded[model], row["hour_start"])
            if version is None:
                unpriced.setdefault(model, row["hour_start"])    # keep going: name every unpriced model, not just the first
                continue
            micros = _rate(version, row["tokens_in"], row["tokens_out"])
            line_count += 1
            total += micros
            if not row["past"]:
                before_count += 1
                before_micros += micros
            elif limit is None or len(page) < limit:
                page.append({"model": model, "hour_start": row["hour_start"], "tokens_in": row["tokens_in"],
                             "tokens_out": row["tokens_out"], "price_version": version["version"], "micros": micros})
                last = (row["hour_start"], model)
    if deadline is not None and time.monotonic() > deadline:     # the last row may have been the slow one
        raise psycopg.errors.QueryCanceled("the charges pass ran past the statement timeout")
    if unpriced:                                     # all or nothing: no partial bill, whichever page was asked for
        raise NoPrice(_unpriced_message(unpriced))
    if after and (before_count, before_micros) != (after.count_before, after.micros_before):
        raise RangeChanged(f"the {before_count} lines up to the cursor are not the {after.count_before} it was made from")
    kept = before_count + len(page)
    next_cursor = Cursor(last[0], last[1], kept, before_micros + sum(line["micros"] for line in page)) \
        if last and line_count > kept else None
    log.info("charges computed: tenant=%r start=%s end=%s lines=%d kept=%d total_micros=%d",
             tenant_id, start.isoformat(), end.isoformat(), line_count, len(page), total)
    return Page(page, total, line_count, next_cursor)


def charge_lines(conn: psycopg.Connection, tenant_id: str, start: datetime, end: datetime,
                 max_lines: int | None = None) -> tuple[list[dict], int]:
    """Rate every hourly rollup row of the tenant with `start <= hour_start < end`, each at its own hour_start.

    Returns (lines, total_micros). Each line is rounded on its own (see `rate`) and the total is the
    sum of the lines. A whole hour is rated at the price in force at its start, so a version applies from the first
    hour that starts at or after its `effective_from`; usage inside an hour is never split between two prices.
    Raises NoPrice if any row has no price in force at its hour, and TooManyLines if `max_lines` is given and the range
    has more rows than that (checked while reading, before the lines are built).
    """
    page = _charge_pass(conn, tenant_id, start, end, after=None, limit=None, max_rows=max_lines)
    return page.lines, page.total_micros


def charge_page(conn: psycopg.Connection, tenant_id: str, start: datetime, end: datetime, limit: int,
                after: Cursor | None = None) -> Page:
    """At most `limit` lines of the range, those after `after` (the start when None). The total and the line count are the
    whole range's, and a missing price anywhere in it raises NoPrice, as for `charge_lines`. A pass is made per page."""
    return _charge_pass(conn, tenant_id, start, end, after=after, limit=limit, max_rows=None)


def encode_cursor(tenant_id: str, start: datetime, end: datetime, cursor: Cursor) -> str:
    """Opaque text for the client; not signed, it holds nothing secret and a change to it is caught by the counts."""
    body = {"t": tenant_id, "s": _offset_text(start), "e": _offset_text(end), "h": _offset_text(cursor.hour_start), "m": cursor.model,
            "n": cursor.count_before, "u": cursor.micros_before}
    return base64.urlsafe_b64encode(json.dumps(body, separators=(",", ":")).encode()).decode()


def decode_cursor(text: str, tenant_id: str, start: datetime, end: datetime) -> Cursor:
    """The cursor in `text`. ValueError if it is malformed, ForeignCursor if it was made for another tenant or range."""
    try:
        body = json.loads(base64.b64decode(text.encode("ascii"), altchars=b"-_", validate=True))
        if not isinstance(body, dict) or set(body) != set(CURSOR_FIELDS):
            raise ValueError("wrong fields")
        for name in ("t", "s", "e", "h", "m"):
            if not isinstance(body[name], str):
                raise ValueError(f"{name} is not text")
        for name in ("n", "u"):
            if type(body[name]) is not int or body[name] < 0:
                raise ValueError(f"{name} is not a count")
        times = [datetime.fromisoformat(body[name]) for name in ("s", "e", "h")]
        if any(t.tzinfo is None for t in times):
            raise ValueError("a time has no zone")
        # The same rules as the model of an ingested event: a NUL or a lone surrogate would be an error inside psycopg (a 500).
        body["m"].encode("utf-8")
        if not 1 <= len(body["m"]) <= MAX_TEXT or "\x00" in body["m"]:
            raise ValueError("bad model")
    except (ValueError, TypeError) as err:                  # binascii.Error, UnicodeError and JSONDecodeError are ValueErrors
        raise ValueError("malformed cursor") from err
    if (body["t"], times[0], times[1]) != (tenant_id, start, end):
        raise ForeignCursor("foreign cursor: it was made for another tenant or range")
    if not start <= times[2] < end:
        raise ValueError("the cursor's hour is outside the range")
    return Cursor(times[2], body["m"], body["n"], body["u"])


CURSOR_FIELDS = ("t", "s", "e", "h", "m", "n", "u")
# Worst case: tenant and model of MAX_TEXT astral characters, each escaped as a 12-character surrogate pair in the JSON,
# plus the fixed fields, then base64's 4/3. Under 8192, so every cursor the service issues is one it accepts.
CURSOR_MAX_LENGTH = 8192


def _offset_text(value: datetime) -> str:
    """The aware value as it was received, with its own offset. Not converted to UTC: `astimezone` overflows for an instant
    whose UTC date is outside year 1 to 9999 (`9999-12-31T23:59:59-05:00`), a range the route accepts. The reader compares
    instants, so the same instant in another offset is the same range."""
    return value.isoformat()


MAX_RANGE = timedelta(days=366)           # a wider range is refused: ask for the period in pieces
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


def unclosed_hours(conn: psycopg.Connection, tenant_id: str, start: datetime, end: datetime) -> tuple[list[datetime], bool]:
    """The earliest UNCLOSED_LIMIT hours in [start, end) that hold usage the charges do not include yet, and whether there
    were more (the list is cut there, so the flag is how a caller knows)."""
    rows = conn.execute(UNCLOSED_HOURS, {"tenant": tenant_id, "start": start, "end": end, "limit": UNCLOSED_LIMIT + 1})
    hours = [r["hour_start"] for r in rows.fetchall()]
    return hours[:UNCLOSED_LIMIT], len(hours) > UNCLOSED_LIMIT


ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _iso_text(value: object) -> object:
    """Only ISO 8601 text: a bare number is read as Unix seconds by the datetime parser (`?start=5` is 1970), and a
    caller who sends one almost certainly meant something else. Every spelling that starts YYYY-MM-DD passes unchanged."""
    if not isinstance(value, str) or not ISO_DATE.match(value):
        raise PydanticCustomError("iso_datetime", "must be ISO 8601 text with a time zone, such as 2026-11-01T00:00:00Z")
    return value


IsoDatetime = Annotated[datetime, BeforeValidator(_iso_text)]


@router.get("/v1/tenants/{tenant_id}/charges")
def get_charges(tenant_id: str, start: IsoDatetime, end: IsoDatetime, limit: int | None = Query(None, ge=1, le=MAX_PAGE),
                cursor: str | None = Query(None, max_length=CURSOR_MAX_LENGTH),
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
    after = None
    if cursor is not None:
        if limit is None:
            raise HTTPException(status_code=422, detail="a cursor needs a limit")
        try:
            after = decode_cursor(cursor, tenant_id, start, end)
        except (ValueError, ForeignCursor) as err:
            raise HTTPException(status_code=422, detail=str(err)) from err
    # Opened here, not in a dependency (as in `ingest_usage`): a dependency's connection stays open while the request waits
    # for a free worker thread, and a refused request above never needs one. A database error reaches the handler in main.py.
    with db.connect() as conn:
        # One snapshot for the unclosed hours, the rows and the prices: an hour that closes while this runs is in all or none.
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        unclosed, unclosed_cut = unclosed_hours(conn, tenant_id, start, end)
        try:
            if limit is None:
                lines, total = charge_lines(conn, tenant_id, start, end, max_lines=LINE_CAP)
                page = Page(lines, total, len(lines), None)
            else:
                page = charge_page(conn, tenant_id, start, end, limit, after)
        except NoPrice as err:
            log.error("charges refused: %s", err)
            raise HTTPException(status_code=409, detail=str(err)) from err
        except TooManyLines as err:
            raise HTTPException(status_code=422, detail=f"{err}: page with limit (at most {MAX_PAGE}), then cursor") from err
        except RangeChanged as err:
            log.warning("charges refused: range changed under cursor: %s", err)
            raise HTTPException(status_code=409, detail=f"range changed, restart: {err}") from err
    if unclosed:
        log.info("charges partial: tenant=%r %d hour(s) with usage are not closed yet, first %s", tenant_id, len(unclosed),
                 unclosed[0].isoformat())
    return {"tenant_id": tenant_id, "start": start, "end": end, "lines": page.lines, "total_micros": page.total_micros,
            "line_count": page.line_count,
            "next_cursor": encode_cursor(tenant_id, start, end, page.next_cursor) if page.next_cursor else None,
            "unclosed_hours": unclosed, "unclosed_hours_truncated": unclosed_cut}
