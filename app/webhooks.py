"""Stripe test-mode webhooks. Owner: dev B.

POST /v1/stripe/webhook verifies the Stripe-Signature header, then updates the `entitlements` record
for `invoice.payment_failed` and `customer.subscription.{created,updated,deleted}`. Each event id is
handled once, in the same transaction as the entitlement change. Nothing is called on Stripe.
"""
import logging
import os
from datetime import datetime, timedelta, timezone

import psycopg
import stripe
from fastapi import APIRouter, Depends, Header, HTTPException, Request

from app.db import connect

log = logging.getLogger(__name__)

router = APIRouter()

SUBSCRIPTION_EVENTS = {"customer.subscription.created", "customer.subscription.updated", "customer.subscription.deleted"}
# Only these Stripe statuses grant access. Any other status (canceled, unpaid, incomplete, paused, a new one) denies.
STATUS_MAP = {"active": "active", "trialing": "active", "past_due": "past_due"}


async def raw_body(request: Request) -> bytes:
    """The request body exactly as sent: the signature covers the raw bytes, not parsed JSON."""
    return await request.body()


def grace_hours() -> int:
    raw = os.environ.get("GRACE_PERIOD_HOURS", "72")
    try:
        hours = int(raw)
    except ValueError:
        hours = -1
    if not 0 <= hours <= 8760:
        log.error("webhook refused: GRACE_PERIOD_HOURS=%r is not an integer from 0 to 8760", raw)
        raise HTTPException(status_code=503, detail="grace period not configured")
    return hours


def verify(payload: bytes, signature: str | None) -> dict:
    """The event as a plain dict, or HTTPException 503 (no secret configured) / 400 (unsigned, bad or stale signature)."""
    secret = os.environ.get("STRIPE_WEBHOOK_SECRET")
    if not secret:
        log.error("webhook refused: STRIPE_WEBHOOK_SECRET is not set")
        raise HTTPException(status_code=503, detail="webhook secret not configured")
    if not signature:
        log.warning("webhook refused: no Stripe-Signature header")
        raise HTTPException(status_code=400, detail="missing Stripe-Signature header")
    try:
        return stripe.Webhook.construct_event(payload, signature, secret).to_dict()
    except (ValueError, stripe.SignatureVerificationError) as err:
        log.warning("webhook refused: %s", err)
        raise HTTPException(status_code=400, detail="invalid signature or payload") from err


def _lock_entitlement(conn: psycopg.Connection, column: str, value: str) -> dict | None:
    return conn.execute(f"SELECT * FROM entitlements WHERE {column} = %s FOR UPDATE", (value,)).fetchone()


def _apply_subscription(conn: psycopg.Connection, event: dict, created: datetime) -> tuple[str, str | None]:
    sub = event["data"]["object"]
    tenant_id = (sub.get("metadata") or {}).get("tenant_id")
    if not tenant_id:
        return "ignored", None
    # Serialize per tenant even when no row exists yet, so a concurrent first insert cannot be overwritten by an older event.
    conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (tenant_id,))
    row = _lock_entitlement(conn, "tenant_id", tenant_id)
    if row and row["last_event_at"] and created < row["last_event_at"]:
        return "stale", tenant_id
    if (event["type"] == "customer.subscription.deleted" and row and row["stripe_subscription_id"]
            and row["stripe_subscription_id"] != sub.get("id")):
        # The end of a replaced subscription must not cancel the current one ("stale" = recorded as handled, no change).
        return "stale", tenant_id
    # A deleted subscription is canceled whatever status its payload carries (fail closed).
    status = "canceled" if event["type"] == "customer.subscription.deleted" else STATUS_MAP.get(sub.get("status"), "canceled")
    items = (sub.get("items") or {}).get("data") or [{}]
    plan = (items[0].get("price") or {}).get("id") or (row and row["plan"])
    payment_failed_at, grace_until = (row["payment_failed_at"], row["grace_until"]) if row else (None, None)
    if status == "active":
        payment_failed_at = grace_until = None
    conn.execute(
        "INSERT INTO entitlements (tenant_id, stripe_customer_id, stripe_subscription_id, plan, status,"
        " payment_failed_at, grace_until, last_event_at)"
        " VALUES (%(t)s, %(c)s, %(s)s, %(p)s, %(st)s, %(pf)s, %(g)s, %(le)s)"
        " ON CONFLICT (tenant_id) DO UPDATE SET stripe_customer_id = EXCLUDED.stripe_customer_id,"
        " stripe_subscription_id = EXCLUDED.stripe_subscription_id, plan = EXCLUDED.plan, status = EXCLUDED.status,"
        " payment_failed_at = EXCLUDED.payment_failed_at, grace_until = EXCLUDED.grace_until,"
        " last_event_at = EXCLUDED.last_event_at, updated_at = now()",
        {"t": tenant_id, "c": sub.get("customer"), "s": sub.get("id"), "p": plan, "st": status,
         "pf": payment_failed_at, "g": grace_until, "le": created})
    return "processed", tenant_id


def _apply_payment_failed(conn: psycopg.Connection, event: dict, created: datetime) -> tuple[str, str | None]:
    customer = event["data"]["object"].get("customer")
    row = _lock_entitlement(conn, "stripe_customer_id", customer) if customer else None
    if row is None:
        return "ignored", None
    tenant_id = row["tenant_id"]
    if row["last_event_at"] and created < row["last_event_at"]:
        if row["status"] == "past_due" and row["grace_until"] is None:
            # A newer past_due subscription event arrived first: the older invoice failure still opens the grace window.
            conn.execute(
                "UPDATE entitlements SET payment_failed_at = %s, grace_until = %s, updated_at = now() WHERE tenant_id = %s",
                (created, created + timedelta(hours=grace_hours()), tenant_id))
            return "processed", tenant_id
        return "stale", tenant_id
    if row["status"] == "canceled":
        # A failed payment must not reopen access: only a subscription event can make a tenant active again.
        return "ignored", tenant_id
    if row["status"] == "past_due" and row["grace_until"] is not None:
        # Grace runs from the FIRST failure of a delinquency. Stripe retries a failed invoice for days: a retry must
        # not slide the window forward. An active subscription event clears it, so a new delinquency starts afresh.
        conn.execute("UPDATE entitlements SET last_event_at = %s, updated_at = now() WHERE tenant_id = %s",
                     (created, tenant_id))
        log.info("webhook repeated failure: tenant=%s keeps grace_until=%s", tenant_id, row["grace_until"].isoformat())
        return "processed", tenant_id
    conn.execute(
        "UPDATE entitlements SET status = 'past_due', payment_failed_at = %s, grace_until = %s,"
        " last_event_at = %s, updated_at = now() WHERE tenant_id = %s",
        (created, created + timedelta(hours=grace_hours()), created, tenant_id))
    return "processed", tenant_id


HANDLERS = {"invoice.payment_failed": _apply_payment_failed, **{name: _apply_subscription for name in SUBSCRIPTION_EVENTS}}


def handle_event(conn: psycopg.Connection, event: dict) -> str:
    """Apply one verified event in the open transaction and return its outcome. The caller commits or rolls back."""
    try:
        if not isinstance(event["id"], str) or not isinstance(event["type"], str):
            raise TypeError("event id and type must be strings")
        if not isinstance(event["data"]["object"], dict):
            raise TypeError("event data.object must be an object")
        created = datetime.fromtimestamp(event["created"], tz=timezone.utc)
    except (KeyError, TypeError, ValueError, OverflowError, OSError) as err:
        log.warning("webhook refused: malformed event: %r", err)
        raise HTTPException(status_code=400, detail="malformed event") from err
    handler = HANDLERS.get(event["type"])
    if handler is None:
        log.info("webhook ignored: event=%s type=%s is not handled", event["id"], event["type"])
        return "ignored"
    inserted = conn.execute(
        "INSERT INTO stripe_events (event_id, type) VALUES (%s, %s) ON CONFLICT (event_id) DO NOTHING RETURNING event_id",
        (event["id"], event["type"])).fetchone()
    if inserted is None:
        log.info("webhook duplicate: event=%s type=%s", event["id"], event["type"])
        return "duplicate"
    outcome, tenant_id = handler(conn, event, created)
    log.info("webhook %s: event=%s type=%s tenant=%s", outcome, event["id"], event["type"], tenant_id)
    return outcome


@router.post("/v1/stripe/webhook")
def stripe_webhook(payload: bytes = Depends(raw_body),
                   stripe_signature: str | None = Header(default=None)) -> dict[str, str]:
    event = verify(payload, stripe_signature)
    with connect() as conn:                       # opened only after the signature checks out; closed on exit
        outcome = handle_event(conn, event)
        if outcome in ("processed", "stale"):     # stale is recorded as handled: its redelivery is a duplicate
            conn.commit()
        else:                                     # ignored: not handled, so the event id is not recorded
            conn.rollback()
    return {"status": outcome}
