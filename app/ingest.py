"""Usage-event intake. Owner: dev A.

POST /v1/usage stores one event in the append-only `usage_events` log. A resend of the same
(tenant_id, idempotency_key) is counted once. The route sits behind `require_entitlement`.
"""
import logging
from datetime import timezone
from typing import Annotated

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import AfterValidator, AwareDatetime, BaseModel, Field, StrictInt, field_validator

from app import db
from app.edge import require_entitlement
from app.rollup import lock_hour_shared

# uvicorn configures this logger at INFO, so these lines show in the server output. The client's idempotency key
# is logged with %r: a newline in it must not start a fake log line.
log = logging.getLogger("uvicorn.error")

MAX_BODY_BYTES = 64 * 1024      # an event is a few hundred bytes; app/bodylimit.py refuses more with 413 before parsing it


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
        # Opened here, not in a dependency: a dependency's connection stays open while the request waits for a
        # free worker thread, so a burst of requests used up Postgres's connection slots.
        with db.connect() as conn:
            return _store(event, response, tenant, conn)
    except psycopg.errors.QueryCanceled:  # a statement past the timeout: the handler in app/main.py answers 504
        raise
    except psycopg.OperationalError as err:  # database down or dropped part way: the same key is safe to resend
        raise _unavailable(err) from err


def _store(event: UsageEvent, response: Response, tenant: str, conn: psycopg.Connection) -> dict[str, str | int | bool]:
    params = event.model_dump()
    db.limit_lock_wait(conn)        # a held hour lock is a 503 after db.LOCK_WAIT, not a pinned worker thread
    # Shared lock on the event's hour, held until commit: close_hour waits for this insert, and an
    # insert that arrives while the hour is closing waits for the close and then sees it as closed.
    lock_hour_shared(conn, event.occurred_at)
    inserted = conn.execute(INSERT_EVENT, params).fetchone()
    if inserted is not None:
        conn.commit()
        log.info("usage accepted: tenant=%s key=%r event_id=%s late=%s",
                 tenant, event.idempotency_key, inserted["id"], inserted["late"])
        return {"status": "accepted", "event_id": inserted["id"], "late": inserted["late"]}

    # The key already exists. Read it back (the row is committed: the INSERT waited for any
    # concurrent writer) and compare payloads.
    existing = conn.execute(SELECT_EVENT, params).fetchone()
    conn.commit()
    same = (existing["model"], existing["tokens_in"], existing["tokens_out"], existing["occurred_at"]) == (
        event.model, event.tokens_in, event.tokens_out, event.occurred_at)
    if not same:
        log.warning("usage conflict: tenant=%s key=%r reused with a different payload (event_id=%s)",
                    tenant, event.idempotency_key, existing["id"])
        raise HTTPException(status_code=409, detail="idempotency_key was already used with a different payload")
    log.info("usage duplicate: tenant=%s key=%r event_id=%s late=%s",
             tenant, event.idempotency_key, existing["id"], existing["late"])
    response.status_code = 200
    return {"status": "duplicate", "event_id": existing["id"], "late": existing["late"]}
