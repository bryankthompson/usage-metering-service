"""Usage-event intake. Owner: dev A.

POST /v1/usage stores one event in the append-only `usage_events` log. A resend of the same
(tenant_id, idempotency_key) is counted once. The route sits behind `require_entitlement`.
"""
import logging

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import AwareDatetime, BaseModel, Field, StrictInt

from app.db import get_conn
from app.edge import require_entitlement
from app.rollup import lock_hour_shared

# uvicorn configures this logger at INFO, so these lines show in the server output.
log = logging.getLogger("uvicorn.error")
router = APIRouter()

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
RETURNING id
"""

SELECT_EVENT = """
SELECT id, model, tokens_in, tokens_out, occurred_at
FROM usage_events WHERE tenant_id = %(tenant_id)s AND idempotency_key = %(idempotency_key)s
"""


class UsageEvent(BaseModel):
    tenant_id: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1, max_length=200)
    model: str = Field(min_length=1)
    tokens_in: StrictInt = Field(ge=0)
    tokens_out: StrictInt = Field(ge=0)
    occurred_at: AwareDatetime  # a naive timestamp is a 422


@router.post("/v1/usage", status_code=201)
def ingest_usage(
    event: UsageEvent,
    response: Response,
    tenant: str = Depends(require_entitlement),
    conn: psycopg.Connection = Depends(get_conn),
) -> dict[str, str | int]:
    if event.tenant_id != tenant:
        log.warning("usage rejected: body tenant %r is not the entitled tenant %r", event.tenant_id, tenant)
        raise HTTPException(status_code=403, detail="tenant_id does not match the entitled tenant")

    params = event.model_dump()
    # Shared lock on the event's hour, held until commit: close_hour waits for this insert, and an
    # insert that arrives while the hour is closing waits for the close and then sees it as closed.
    lock_hour_shared(conn, event.occurred_at)
    inserted = conn.execute(INSERT_EVENT, params).fetchone()
    if inserted is not None:
        conn.commit()
        log.info("usage accepted: tenant=%s key=%s event_id=%s", tenant, event.idempotency_key, inserted["id"])
        return {"status": "accepted", "event_id": inserted["id"]}

    # The key already exists. Read it back (the row is committed: the INSERT waited for any
    # concurrent writer) and compare payloads.
    existing = conn.execute(SELECT_EVENT, params).fetchone()
    conn.commit()
    same = (existing["model"], existing["tokens_in"], existing["tokens_out"], existing["occurred_at"]) == (
        event.model, event.tokens_in, event.tokens_out, event.occurred_at)
    if not same:
        log.warning("usage conflict: tenant=%s key=%s reused with a different payload (event_id=%s)",
                    tenant, event.idempotency_key, existing["id"])
        raise HTTPException(status_code=409, detail="idempotency_key was already used with a different payload")
    log.info("usage duplicate: tenant=%s key=%s event_id=%s", tenant, event.idempotency_key, existing["id"])
    response.status_code = 200
    return {"status": "duplicate", "event_id": existing["id"]}
