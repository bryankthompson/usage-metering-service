"""The API edge check. Owner: dev B.

`require_entitlement` is a FastAPI dependency that resolves the calling tenant and lets the request
through only when that tenant is entitled. It fails closed: an unknown tenant, a lapsed grace period
or a database error all deny. Tenant identity here is the `X-Tenant-Id` header, a stand-in for real
authentication (anyone can send any header: do not expose this edge without a real identity in front).

Tests of other modules override it: `app.dependency_overrides[require_entitlement] = lambda: "tenant"`.
"""
import logging
from datetime import datetime, timezone

import psycopg
from fastapi import HTTPException, Request

from app.db import connect

log = logging.getLogger(__name__)


def now() -> datetime:
    """The clock. Tests replace this function instead of sleeping."""
    return datetime.now(timezone.utc)


def _deny(status_code: int, tenant_id: str | None, reason: str) -> HTTPException:
    log.warning("edge denied: tenant=%s status=%s reason=%s", tenant_id, status_code, reason)
    return HTTPException(status_code=status_code, detail=reason)


def require_entitlement(request: Request) -> str:
    """Return the entitled tenant id, or raise HTTPException (401 no tenant, 402 not entitled, 503 cannot tell).

    The entitlement is read on its own connection, so a database that is down fails here as 503 and
    never lets the request through.
    """
    tenant_id = (request.headers.get("x-tenant-id") or "").strip()
    if not tenant_id:
        raise _deny(401, None, "X-Tenant-Id header is required")
    try:
        with connect() as conn:
            row = conn.execute(
                "SELECT status, grace_until FROM entitlements WHERE tenant_id = %s", (tenant_id,)).fetchone()
    except psycopg.Error as err:
        log.error("edge cannot read entitlements: %s", err)
        raise _deny(503, tenant_id, "entitlement check unavailable") from err
    if row is None:
        raise _deny(402, tenant_id, "no entitlement")
    if row["status"] == "active":
        return tenant_id
    if row["status"] == "past_due" and row["grace_until"] is not None and now() <= row["grace_until"]:
        return tenant_id
    raise _deny(402, tenant_id, f"not entitled: status {row['status']}")
