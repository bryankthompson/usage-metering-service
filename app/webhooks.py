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

from app.db import limit_lock_wait, request_connection

log = logging.getLogger(__name__)

router = APIRouter()

SUBSCRIPTION_EVENTS = {"customer.subscription.created", "customer.subscription.updated", "customer.subscription.deleted"}
# Only these Stripe statuses grant access. Any other status (canceled, unpaid, incomplete, paused, a new one) denies.
STATUS_MAP = {"active": "active", "trialing": "active", "past_due": "past_due"}


MAX_BODY_BYTES = 1024 * 1024        # a Stripe event is a few KB; 1 MiB leaves room, and the endpoint is public


async def raw_body(request: Request) -> bytes:
    """The request body exactly as sent: the signature covers the raw bytes, not parsed JSON.

    Read before the signature is checked, so it is capped: a 600 MB unsigned body took the API to 2.4 GiB of memory.
    """
    body = bytearray()
    async for chunk in request.stream():                     # counted as it arrives, so memory stays near the cap
        body += chunk
        if len(body) > MAX_BODY_BYTES:
            raise HTTPException(status_code=413, detail="webhook body too large")
    return bytes(body)


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
    if not secret or not secret.strip():                      # a blank secret is no secret: 503, not a "bad signature" 400
        log.error("webhook refused: STRIPE_WEBHOOK_SECRET is not set")
        raise HTTPException(status_code=503, detail="webhook secret not configured")
    if not signature:
        log.warning("webhook refused: no Stripe-Signature header")
        raise HTTPException(status_code=400, detail="missing Stripe-Signature header")
    if not signature.isascii():
        # The library compares signatures with hmac.compare_digest, which raises TypeError (a 500) on non-ASCII text.
        log.warning("webhook refused: Stripe-Signature header is not ASCII")
        raise HTTPException(status_code=400, detail="invalid signature or payload")
    try:
        return stripe.Webhook.construct_event(payload, signature, secret).to_dict()
    except (ValueError, AttributeError, RecursionError, stripe.SignatureVerificationError) as err:
        # Only a body that is correctly signed gets parsed, and the library raises AttributeError for signed JSON that
        # is not an object (`[]`, `null`, `123`) and RecursionError for JSON nested thousands deep.
        log.warning("webhook refused: %s", err)
        raise HTTPException(status_code=400, detail="invalid signature or payload") from err


MAX_TENANT_ID = 200                 # the same limit as tenant_id on POST /v1/usage, so every stored tenant can post usage


def _text(value: object) -> str | None:
    """`value` when PostgreSQL can store it (a str that encodes as UTF-8, without NUL), else None.

    Signed does not mean well-formed: JSON allows a lone surrogate ("\\ud800"), which cannot be encoded."""
    if not isinstance(value, str) or "\x00" in value:
        return None
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return None
    return value


def _tenant_id(value: object) -> str | None:
    """`value` when the edge and ingest can both admit it: 1 to 200 characters, no ASCII control character, no space or
    tab at either end (the edge trims those from the header, so such a tenant could never match its own header)."""
    text = _text(value)
    if text is None or not 1 <= len(text) <= MAX_TENANT_ID or text != text.strip(" \t"):
        return None
    return None if any(ord(c) < 0x20 or ord(c) == 0x7F for c in text) else text


def _lock_entitlement(conn: psycopg.Connection, column: str, value: str) -> dict | None:
    return conn.execute(f"SELECT * FROM entitlements WHERE {column} = %s FOR UPDATE", (value,)).fetchone()


def _apply_subscription(conn: psycopg.Connection, event: dict, created: datetime) -> tuple[str, str | None]:
    sub = event["data"]["object"]
    metadata = sub.get("metadata")
    raw_tenant = metadata.get("tenant_id") if isinstance(metadata, dict) else None
    tenant_id = _tenant_id(raw_tenant)
    if not tenant_id and _text(raw_tenant) and conn.execute(
            "SELECT 1 FROM entitlements WHERE tenant_id = %s", (raw_tenant,)).fetchone():
        tenant_id = raw_tenant   # a row stored before the id rule (or by hand) stays reachable: a lost cancel is never retried
    if not tenant_id:
        if raw_tenant is not None:
            log.warning("webhook ignored: tenant_id %.80r is not usable (1 to %d characters, no control characters, "
                        "no space or tab at either end)", raw_tenant, MAX_TENANT_ID)
        return "ignored", None
    # Serialize per tenant even when no row exists yet, so a concurrent first insert cannot be overwritten by an older event.
    conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (tenant_id,))
    row = _lock_entitlement(conn, "tenant_id", tenant_id)
    event_sub_id = _text(sub.get("id"))
    # A delete of the subscription the row holds is terminal (a canceled subscription id is never reopened), so an
    # older `created` cannot make it stale: a payment failure created after the cancel and applied first would
    # otherwise hide it. A delete of another id (an old subscription, delivered late) or a row with no id is judged by the clock.
    terminal = bool(event["type"] == "customer.subscription.deleted" and row and event_sub_id
                    and event_sub_id == row["stripe_subscription_id"])
    if row and row["last_event_at"] and created < row["last_event_at"] and not terminal:
        return "stale", tenant_id
    if (row and row["last_event_at"] == created and row["status"] == "canceled"
            and event["type"] != "customer.subscription.deleted"):
        # Stripe's `created` has one-second resolution and delivery order is not guaranteed, so a late `updated(active)` can
        # carry the cancel's own second. Ties apply in arrival order except this one: a tie never undoes a cancel.
        log.warning("webhook tie after cancel: event=%r type=%r tenant=%r shares the second of the cancel and is stale",
                    event["id"], event["type"], tenant_id)
        return "stale", tenant_id
    # A deleted subscription is canceled whatever status its payload carries (fail closed).
    raw_status = sub.get("status")
    status = "canceled" if event["type"] == "customer.subscription.deleted" else STATUS_MAP.get(
        raw_status if isinstance(raw_status, str) else None, "canceled")
    items, price = sub.get("items"), None
    data = items.get("data") if isinstance(items, dict) else None
    if isinstance(data, list) and data and isinstance(data[0], dict):
        price = data[0].get("price")
    # A field that is missing or unusable keeps the stored value instead of erasing it.
    plan = (_text(price.get("id")) if isinstance(price, dict) else None) or (row and row["plan"])
    customer = _text(sub.get("customer")) or (row and row["stripe_customer_id"])
    subscription_id = event_sub_id or (row and row["stripe_subscription_id"])
    last_event_at = max(created, row["last_event_at"]) if row and row["last_event_at"] else created   # never backwards
    payment_failed_at, grace_until = (row["payment_failed_at"], row["grace_until"]) if row else (None, None)
    if status in ("active", "canceled"):               # a cancel leaves no grace window to revive later
        payment_failed_at = grace_until = None
    conn.execute(
        "INSERT INTO entitlements (tenant_id, stripe_customer_id, stripe_subscription_id, plan, status,"
        " payment_failed_at, grace_until, last_event_at)"
        " VALUES (%(t)s, %(c)s, %(s)s, %(p)s, %(st)s, %(pf)s, %(g)s, %(le)s)"
        " ON CONFLICT (tenant_id) DO UPDATE SET stripe_customer_id = EXCLUDED.stripe_customer_id,"
        " stripe_subscription_id = EXCLUDED.stripe_subscription_id, plan = EXCLUDED.plan, status = EXCLUDED.status,"
        " payment_failed_at = EXCLUDED.payment_failed_at, grace_until = EXCLUDED.grace_until,"
        " last_event_at = EXCLUDED.last_event_at, updated_at = now()",
        {"t": tenant_id, "c": customer or None, "s": subscription_id or None, "p": plan or None, "st": status,
         "pf": payment_failed_at, "g": grace_until, "le": last_event_at})
    return "processed", tenant_id


def _apply_payment_failed(conn: psycopg.Connection, event: dict, created: datetime) -> tuple[str, str | None]:
    customer = _text(event["data"]["object"].get("customer"))
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
        log.info("webhook repeated failure: tenant=%r keeps grace_until=%s", tenant_id, row["grace_until"].isoformat())
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
        if _text(event["id"]) is None:
            raise ValueError("event id is not storable text (NUL byte or lone surrogate)")
        if not isinstance(event["data"]["object"], dict):
            raise TypeError("event data.object must be an object")
        created = datetime.fromtimestamp(event["created"], tz=timezone.utc)
    except (KeyError, TypeError, ValueError, OverflowError, OSError) as err:
        log.warning("webhook refused: malformed event: %r", err)
        raise HTTPException(status_code=400, detail="malformed event") from err
    handler = HANDLERS.get(event["type"])
    if handler is None:
        log.info("webhook ignored: event=%r type=%r is not handled", event["id"], event["type"])
        return "ignored"
    limit_lock_wait(conn)      # a held tenant or row lock is a 503 (Stripe retries) after db.LOCK_WAIT, not a pinned thread
    inserted = conn.execute(
        "INSERT INTO stripe_events (event_id, type) VALUES (%s, %s) ON CONFLICT (event_id) DO NOTHING RETURNING event_id",
        (event["id"], event["type"])).fetchone()
    if inserted is None:
        log.info("webhook duplicate: event=%r type=%r", event["id"], event["type"])
        return "duplicate"
    outcome, tenant_id = handler(conn, event, created)
    log.info("webhook %s: event=%r type=%r tenant=%r", outcome, event["id"], event["type"], tenant_id)
    return outcome


@router.post("/v1/stripe/webhook")
def stripe_webhook(payload: bytes = Depends(raw_body),
                   stripe_signature: str | None = Header(default=None)) -> dict[str, str]:
    event = verify(payload, stripe_signature)
    with request_connection() as conn:           # taken only after the signature checks out; returned on exit
        outcome = handle_event(conn, event)
        if outcome in ("processed", "stale"):     # stale is recorded as handled: its redelivery is a duplicate
            conn.commit()
        else:                                     # ignored: not handled, so the event id is not recorded
            conn.rollback()
    return {"status": outcome}
