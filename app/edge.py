"""The API edge check. Owner: dev B.

`require_entitlement` is a FastAPI dependency that resolves the calling tenant and lets the request
through only when that tenant is entitled. It fails closed: an unknown tenant, a lapsed grace period
or a database error all deny. Tenant identity here is the `X-Tenant-Id` header, a stand-in for real
authentication.

Until dev B lands the real check, this stub denies everything (fail closed from the first commit).
Tests of other modules override it: `app.dependency_overrides[require_entitlement] = lambda: "tenant"`.
"""
from fastapi import HTTPException


def require_entitlement() -> str:
    """Return the entitled tenant id, or raise HTTPException (402 not entitled, 503 cannot tell)."""
    raise HTTPException(status_code=503, detail="edge check not implemented")
