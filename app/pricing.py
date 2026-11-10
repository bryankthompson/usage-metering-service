"""Versioned price table and rating. Owner: dev B.

Money is an integer count of micro-USD (1 USD = 1,000,000). There is no float and no true division
anywhere in this module.

Prices are per model and versioned: a new price is a new row, a stored version is never changed
(a database trigger enforces it). `price_at` picks the version in force at a given instant, so a
charge for a past hour is rated with the price that applied then.
"""
import logging
from datetime import datetime

import psycopg
from fastapi import APIRouter, Depends, HTTPException

from app.db import get_conn
from app.edge import require_entitlement

log = logging.getLogger(__name__)

router = APIRouter()


class NoPrice(Exception):
    """No price version is in force for the model at that time. Never rated as zero."""


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


def add_price_version(conn: psycopg.Connection, model: str, effective_from: datetime,
                      input_micros_per_1k: int, output_micros_per_1k: int) -> int:
    """Store the next version (1, 2, ...) for `model` and return its number. Commits.

    Two writers racing for the same number collide on the primary key and one raises; neither
    overwrites the other.
    """
    _require_aware(effective_from, "effective_from")
    row = conn.execute(
        "INSERT INTO price_versions (model, version, effective_from, input_micros_per_1k, output_micros_per_1k)"
        " SELECT %s, COALESCE(MAX(version), 0) + 1, %s, %s, %s FROM price_versions WHERE model = %s"
        " RETURNING version",
        (model, effective_from, input_micros_per_1k, output_micros_per_1k, model)).fetchone()
    conn.commit()
    log.info("price version added: model=%s version=%s effective_from=%s in=%s out=%s",
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


def charge_lines(conn: psycopg.Connection, tenant_id: str, start: datetime, end: datetime) -> tuple[list[dict], int]:
    """Rate every hourly rollup row of the tenant with `start <= hour_start < end`, each at its own hour_start.

    Returns (lines, total_micros). Each line is rounded on its own (see `rate`) and the total is the
    sum of the lines. A price change inside the period therefore applies from its effective time.
    Raises NoPrice if any row has no price in force at its hour.
    """
    _require_aware(start, "start")
    _require_aware(end, "end")
    rows = conn.execute(
        "SELECT model, hour_start, tokens_in, tokens_out FROM usage_rollups_hourly"
        " WHERE tenant_id = %s AND hour_start >= %s AND hour_start < %s ORDER BY hour_start, model",
        (tenant_id, start, end)).fetchall()
    lines = []
    for row in rows:
        version = price_at(conn, row["model"], row["hour_start"])
        lines.append({
            "model": row["model"],
            "hour_start": row["hour_start"],
            "tokens_in": row["tokens_in"],
            "tokens_out": row["tokens_out"],
            "price_version": version["version"],
            "micros": _rate(version, row["tokens_in"], row["tokens_out"]),
        })
    total = sum(line["micros"] for line in lines)
    log.info("charges computed: tenant=%s start=%s end=%s lines=%d total_micros=%d",
             tenant_id, start.isoformat(), end.isoformat(), len(lines), total)
    return lines, total


@router.get("/v1/tenants/{tenant_id}/charges")
def get_charges(tenant_id: str, start: datetime, end: datetime,
                caller: str = Depends(require_entitlement),
                conn: psycopg.Connection = Depends(get_conn)) -> dict:
    if caller != tenant_id:
        raise HTTPException(status_code=403, detail="tenant does not match the caller")
    for name, value in (("start", start), ("end", end)):
        if value.tzinfo is None:
            raise HTTPException(status_code=422, detail=f"{name} must be timezone-aware")
    if start >= end:
        raise HTTPException(status_code=422, detail="start must be before end")
    try:
        lines, total = charge_lines(conn, tenant_id, start, end)
    except NoPrice as err:
        log.error("charges refused: %s", err)
        raise HTTPException(status_code=409, detail=str(err)) from err
    return {"tenant_id": tenant_id, "start": start, "end": end, "lines": lines, "total_micros": total}
