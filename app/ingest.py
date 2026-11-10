"""Usage-event intake. Owner: dev A.

POST /v1/usage stores one event in the append-only `usage_events` log. A resend of the same
(tenant_id, idempotency_key) is counted once. The route sits behind `require_entitlement`.
"""
from fastapi import APIRouter, HTTPException

router = APIRouter()


@router.post("/v1/usage")
def ingest_usage() -> None:
    raise HTTPException(status_code=501, detail="not implemented")
