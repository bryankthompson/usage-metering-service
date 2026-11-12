"""Usage-event intake. Owner: dev A.

POST /v1/usage stores one event in the append-only `usage_events` log. A resend of the same
(tenant_id, idempotency_key) is counted once. The route sits behind `require_entitlement`.
"""

import json
import logging
from datetime import timezone
from typing import Annotated

import psycopg
from psycopg.rows import DictRow
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import AfterValidator, AwareDatetime, BaseModel, Field, StrictInt, field_validator, model_validator

from app import db
from app.edge import require_entitlement
from app.rollup import hour_index, lock_hour_shared

# uvicorn configures this logger at INFO, so these lines show in the server output. The client's idempotency key
# is logged with %r: a newline in it must not start a fake log line.
log = logging.getLogger("uvicorn.error")

MAX_BODY_BYTES = 64 * 1024  # an event is a few hundred bytes; app/bodylimit.py refuses more with 413 before parsing it


class QuietValidationRoute(APIRoute):
    """A 422 that does not echo the offending input. FastAPI echoes it, and a NaN, an Infinity or a lone
    surrogate in it cannot be written as JSON, which turned the 422 into a 500."""

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def guarded(request: Request) -> Response:
            try:
                return await handler(request)
            except RequestValidationError as err:
                detail = [{key: e[key] for key in ("type", "loc", "msg")} for e in err.errors()]
                return JSONResponse(status_code=422, content={"detail": detail})

        return guarded


router = APIRouter(route_class=QuietValidationRoute)

# Per-event token ceiling: far above any real call. A larger number is a 422 instead of a database error.
# It does not bound a sum of events: see "Known limit" in docs/ingest.md.
MAX_TOKENS = 10**12
# Longest tenant_id, idempotency_key or model. These are btree key columns (a row over ~2700 bytes is refused
# by Postgres, and a refused rollup row would stop every later hour from closing); 200 characters of any
# script stay well under that.
MAX_TEXT = 200

# One statement does the dedupe and the late check. ON CONFLICT DO NOTHING makes two identical
# requests race safely (one inserts, the other gets no row back). `late` is true when the event's
# UTC hour is already in closed_hours; date_trunc takes 'UTC' so the session time zone cannot move
# the hour boundary.
INSERT_EVENT = """
INSERT INTO usage_events (tenant_id, idempotency_key, model, tokens_in, tokens_out, occurred_at, late)
VALUES (%(tenant_id)s, %(idempotency_key)s, %(model)s, %(tokens_in)s, %(tokens_out)s, %(occurred_at)s,
        EXISTS (SELECT 1 FROM closed_hours
                WHERE hour_start = date_trunc('hour', %(occurred_at)s::timestamptz, 'UTC')))
ON CONFLICT (tenant_id, idempotency_key) DO NOTHING
RETURNING id, late
"""

SELECT_EVENT = """
SELECT id, model, tokens_in, tokens_out, occurred_at, late
FROM usage_events WHERE tenant_id = %(tenant_id)s AND idempotency_key = %(idempotency_key)s
"""


def _storable(text: str) -> str:
    """Postgres text cannot hold a NUL character; stored, it would be a 500. (A lone surrogate is already a 422.)"""
    if "\x00" in text:
        raise ValueError("must not contain a NUL character")
    return text


Text = Annotated[str, Field(min_length=1, max_length=MAX_TEXT), AfterValidator(_storable)]


class UsageEvent(BaseModel):
    tenant_id: Text
    idempotency_key: Text
    model: Text
    tokens_in: StrictInt = Field(ge=0, le=MAX_TOKENS)
    tokens_out: StrictInt = Field(ge=0, le=MAX_TOKENS)
    occurred_at: AwareDatetime  # a naive timestamp is a 422

    @field_validator("occurred_at", mode="before")
    @classmethod
    def _iso_text(cls, value):
        """Pydantic reads a number, or a string of digits, as Unix seconds (5 becomes 1970-01-01T00:00:05Z): a
        client that sends one has a bug, and the event would be billed in the wrong hour without any error."""
        if not isinstance(value, str) or value.strip().lstrip("+-").replace(".", "", 1).isdigit():
            raise ValueError("occurred_at must be an ISO 8601 timestamp with a time zone")
        return value

    @field_validator("occurred_at")
    @classmethod
    def _utc_year_in_range(cls, ts):
        """Stored hours are read back as Python datetimes (years 1 to 9999): an offset that pushes the UTC
        instant outside them would store, then fail on the resend and in the rollup."""
        try:
            ts.astimezone(timezone.utc)
        except OverflowError:
            raise ValueError("occurred_at must be within years 1 to 9999 in UTC") from None
        return ts


def _same_payload(stored, event: UsageEvent) -> bool:
    """Is a stored row (or a dict with the same four keys) the same event as `event`? Both routes decide a resend with this."""
    return (stored["model"], stored["tokens_in"], stored["tokens_out"], stored["occurred_at"]) == (
        event.model,
        event.tokens_in,
        event.tokens_out,
        event.occurred_at,
    )


def _unavailable(err: psycopg.OperationalError) -> HTTPException:
    log.error("usage unavailable: %s", err)
    return HTTPException(status_code=503, detail="database unavailable", headers={"Retry-After": "1"})


@router.post("/v1/usage", status_code=201)
def ingest_usage(
    event: UsageEvent,
    response: Response,
    tenant: str = Depends(require_entitlement),
) -> dict[str, str | int | bool]:
    if event.tenant_id != tenant:
        log.warning("usage rejected: body tenant %r is not the entitled tenant %r", event.tenant_id, tenant)
        raise HTTPException(status_code=403, detail="tenant_id does not match the entitled tenant")
    try:
        # Taken here, not in a dependency: a dependency's connection stays held while the request waits for a free
        # worker thread, which used up Postgres's connection slots, and with a bounded pool it would deadlock.
        with db.request_connection() as conn:
            return _store(event, response, tenant, conn)
    except psycopg.errors.QueryCanceled:  # a statement past the timeout: the handler in app/main.py answers 504
        raise
    except psycopg.OperationalError as err:  # database down or dropped part way: the same key is safe to resend
        raise _unavailable(err) from err


def _store(
    event: UsageEvent, response: Response, tenant: str, conn: psycopg.Connection[DictRow]
) -> dict[str, str | int | bool]:
    params = event.model_dump()
    db.limit_lock_wait(conn)  # a held hour lock is a 503 after db.LOCK_WAIT, not a pinned worker thread
    # Shared lock on the event's hour, held until commit: close_hour waits for this insert, and an
    # insert that arrives while the hour is closing waits for the close and then sees it as closed.
    lock_hour_shared(conn, event.occurred_at)
    inserted = conn.execute(INSERT_EVENT, params).fetchone()
    if inserted is not None:
        conn.commit()
        log.info(
            "usage accepted: tenant=%s key=%r event_id=%s late=%s",
            tenant,
            event.idempotency_key,
            inserted["id"],
            inserted["late"],
        )
        return {"status": "accepted", "event_id": inserted["id"], "late": inserted["late"]}

    # The key already exists. Read it back (the row is committed: the INSERT waited for any
    # concurrent writer) and compare payloads.
    existing = conn.execute(SELECT_EVENT, params).fetchone()
    conn.commit()
    if not _same_payload(existing, event):
        log.warning(
            "usage conflict: tenant=%s key=%r reused with a different payload (event_id=%s)",
            tenant,
            event.idempotency_key,
            existing["id"],  # type: ignore[index]  # fetchone() is a row here: the INSERT conflicted with an existing event
        )
        raise HTTPException(status_code=409, detail="idempotency_key was already used with a different payload")
    log.info(
        "usage duplicate: tenant=%s key=%r event_id=%s late=%s",
        tenant,
        event.idempotency_key,
        existing["id"],  # type: ignore[index]  # fetchone() is a row here: the INSERT conflicted with an existing event
        existing["late"],  # type: ignore[index]  # fetchone() is a row here: the INSERT conflicted with an existing event
    )
    response.status_code = 200
    return {"status": "duplicate", "event_id": existing["id"], "late": existing["late"]}  # type: ignore[index]  # fetchone() is a row here: the INSERT conflicted with an existing event


# --- POST /v1/usage/batch -------------------------------------------------------------------------------------------
# Many events in one transaction. Same dedupe rule as the single route: the unique key (tenant_id, idempotency_key), a
# resend of the same payload is a duplicate, a different payload under the same key is a conflict. All or nothing: any
# invalid, foreign-tenant or conflicting item refuses the whole batch and stores nothing, so a resend is always safe.

MAX_BATCH_EVENTS = 1000
# Each distinct UTC hour in a batch is one advisory lock in Postgres's shared lock table (max_locks_per_transaction x
# max_connections slots, 6,400 by default; when it is full a lock request fails with "out of shared memory", a 503), and
# each lock wait can cost the whole db.LOCK_WAIT (a wait that runs out is a 503), so D hours can wait up to D x 2 s in
# total; the 15 s statement timeout is per statement and each lock is its own statement. A real batch spans a few hours.
MAX_BATCH_HOURS = 48


def _largest_event_json() -> str:
    """The longest event a client can send as ordinary JSON: three 200-character fields of non-BMP characters (4 bytes
    as UTF-8, 12 as the \\ud83d\\ude00 escape Python's json.dumps writes), the largest token counts, the longest
    timestamp spelling (6 fractional digits, a +hh:mm offset). Extra fractional digits or whitespace make a body larger
    but are padding, not data; like the single route's cap, the batch cap refuses them with 413."""
    return json.dumps(
        {
            "tenant_id": "\U0001f600" * MAX_TEXT,
            "idempotency_key": "\U0001f600" * MAX_TEXT,
            "model": "\U0001f600" * MAX_TEXT,
            "tokens_in": MAX_TOKENS,
            "tokens_out": MAX_TOKENS,
            "occurred_at": "9999-12-31T23:59:59.999999+23:59",
        }
    )


# MAX_BATCH_EVENTS of those in json.dumps's default spacing, in the {"events": [...]} envelope: one byte more is 413.
MAX_BATCH_BODY_BYTES = (
    len('{"events": []}') + MAX_BATCH_EVENTS * len(_largest_event_json()) + (MAX_BATCH_EVENTS - 1) * len(", ")
)


class UsageBatch(BaseModel):
    events: list[UsageEvent] = Field(min_length=1, max_length=MAX_BATCH_EVENTS)

    @model_validator(mode="after")
    def _few_hours(self):
        hours = {hour_index(e.occurred_at) for e in self.events}
        if len(hours) > MAX_BATCH_HOURS:
            raise ValueError(f"events span {len(hours)} UTC hours; at most {MAX_BATCH_HOURS} in one batch")
        return self


# The same statement as INSERT_EVENT, over arrays. It is given each key once (repeats inside the batch are resolved in
# Python first), so ORDER BY is a total order: every batch inserts in the same key order, and two overlapping batches
# queue on the unique index instead of deadlocking. `late` is the same expression, evaluated per row.
INSERT_BATCH = """
INSERT INTO usage_events (tenant_id, idempotency_key, model, tokens_in, tokens_out, occurred_at, late)
SELECT b.tenant_id, b.idempotency_key, b.model, b.tokens_in, b.tokens_out, b.occurred_at,
       EXISTS (SELECT 1 FROM closed_hours WHERE hour_start = date_trunc('hour', b.occurred_at, 'UTC'))
FROM unnest(%(tenant_ids)s::text[], %(keys)s::text[], %(models)s::text[], %(tokens_in)s::bigint[],
            %(tokens_out)s::bigint[], %(occurred_at)s::timestamptz[])
     AS b(tenant_id, idempotency_key, model, tokens_in, tokens_out, occurred_at)
ORDER BY b.tenant_id, b.idempotency_key
ON CONFLICT (tenant_id, idempotency_key) DO NOTHING
RETURNING id, idempotency_key, late
"""

SELECT_BATCH_KEYS = """
SELECT id, idempotency_key, model, tokens_in, tokens_out, occurred_at, late
FROM usage_events WHERE tenant_id = %(tenant_id)s AND idempotency_key = ANY(%(keys)s::text[])
"""


@router.post("/v1/usage/batch", status_code=201)
def ingest_usage_batch(
    batch: UsageBatch,
    response: Response,
    tenant: str = Depends(require_entitlement),
) -> dict:
    events = batch.events
    for i, event in enumerate(events):
        if event.tenant_id != tenant:
            log.warning(
                "usage batch rejected: item %d body tenant %r is not the entitled tenant %r", i, event.tenant_id, tenant
            )
            raise HTTPException(status_code=403, detail=f"events[{i}].tenant_id does not match the entitled tenant")
    # A key sent twice in the batch is one event: the first copy is stored, a later identical copy is its duplicate, a
    # later different copy is a conflict. Decided here, not by the INSERT, which would pick an arbitrary copy.
    first: dict[str, int] = {}
    for i, event in enumerate(events):
        j = first.setdefault(event.idempotency_key, i)
        if j != i and not _same_payload(events[j].model_dump(), event):
            log.warning(
                "usage batch conflict: tenant=%s items %d and %d share key %r with different payloads",
                tenant,
                j,
                i,
                event.idempotency_key,
            )
            raise HTTPException(
                status_code=409,
                detail=f"events[{j}] and events[{i}] use the same idempotency_key with different payloads",
            )
    try:
        with db.request_connection() as conn:  # leaving the block on an exception rolls the whole batch back
            results = _store_batch(events, first, tenant, conn)
    except psycopg.errors.QueryCanceled:
        raise
    except psycopg.OperationalError as err:
        raise _unavailable(err) from err
    accepted = sum(r["status"] == "accepted" for r in results)
    log.info(
        "usage batch: tenant=%s events=%d accepted=%d duplicate=%d",
        tenant,
        len(results),
        accepted,
        len(results) - accepted,
    )
    if not accepted:
        response.status_code = 200
    return {"accepted": accepted, "duplicate": len(results) - accepted, "results": results}


def _store_batch(
    events: list[UsageEvent], first: dict[str, int], tenant: str, conn: psycopg.Connection[DictRow]
) -> list[dict]:
    db.limit_lock_wait(conn)
    # One shared lock per distinct hour, each its own statement, in ascending hour order, all BEFORE the INSERT: a lock
    # taken inside the INSERT would be taken after its snapshot, and an event could then be in neither the closed
    # rollup nor `late`. Ascending order keeps two batches from waiting on each other's hours.
    hours = {hour_index(e.occurred_at): e.occurred_at for e in events}
    for index in sorted(hours):
        lock_hour_shared(conn, hours[index])
    firsts = [events[i] for i in first.values()]
    # The instants go in as UTC: an array literal is sent as text, and Postgres refuses a text offset past +-15:59 (the
    # single route sends a datetime as a binary value and has no such limit).
    inserted = {
        row["idempotency_key"]: row
        for row in conn.execute(
            INSERT_BATCH,
            {
                "tenant_ids": [e.tenant_id for e in firsts],
                "keys": [e.idempotency_key for e in firsts],
                "models": [e.model for e in firsts],
                "tokens_in": [e.tokens_in for e in firsts],
                "tokens_out": [e.tokens_out for e in firsts],
                "occurred_at": [e.occurred_at.astimezone(timezone.utc) for e in firsts],
            },
        ).fetchall()
    }
    # A key that did not insert already exists: read those back in one query and compare, exactly as the single route does.
    missing = [e for e in firsts if e.idempotency_key not in inserted]
    stored = {
        row["idempotency_key"]: row
        for row in conn.execute(
            SELECT_BATCH_KEYS, {"tenant_id": tenant, "keys": [e.idempotency_key for e in missing]}
        ).fetchall()
    }
    for e in missing:
        if not _same_payload(stored[e.idempotency_key], e):
            i = first[e.idempotency_key]
            log.warning(
                "usage batch conflict: tenant=%s item %d key=%r reused with a different payload (event_id=%s)",
                tenant,
                i,
                e.idempotency_key,
                stored[e.idempotency_key]["id"],
            )
            raise HTTPException(
                status_code=409, detail=f"events[{i}]: idempotency_key was already used with a different payload"
            )
    return [
        {
            "idempotency_key": e.idempotency_key,
            "status": "accepted" if first[e.idempotency_key] == i and e.idempotency_key in inserted else "duplicate",
            "event_id": (inserted.get(e.idempotency_key) or stored[e.idempotency_key])["id"],
            "late": (inserted.get(e.idempotency_key) or stored[e.idempotency_key])["late"],
        }
        for i, e in enumerate(events)
    ]
