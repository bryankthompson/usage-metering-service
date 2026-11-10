"""Locally signed Stripe test fixtures. The secret is obviously fake; nothing here talks to Stripe."""
import hashlib
import hmac
import json
import time

SECRET = "whsec_test_fake_secret_for_local_tests"


def sign(payload: bytes, secret: str = SECRET, timestamp: int | None = None) -> str:
    """A Stripe-Signature header value: t=<unix>,v1=HMAC-SHA256(secret, "<t>.<payload>")."""
    t = int(time.time()) if timestamp is None else timestamp
    digest = hmac.new(secret.encode(), f"{t}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={t},v1={digest}"


def event(event_id: str, event_type: str, created: int, obj: dict) -> bytes:
    return json.dumps({"id": event_id, "object": "event", "type": event_type, "created": created,
                       "data": {"object": obj}}).encode()


def subscription_event(event_id: str, created: int, tenant_id: str | None = "tenant-1", status: str = "active",
                       customer: str = "cus_test_1", price: str = "price_test_pro",
                       event_type: str = "customer.subscription.updated", sub_id: str = "sub_test_1") -> bytes:
    sub = {"id": sub_id, "object": "subscription", "customer": customer, "status": status,
           "metadata": {"tenant_id": tenant_id} if tenant_id else {},
           "items": {"data": [{"price": {"id": price}}]}}
    return event(event_id, event_type, created, sub)


def payment_failed_event(event_id: str, created: int, customer: str = "cus_test_1") -> bytes:
    return event(event_id, "invoice.payment_failed", created,
                 {"id": "in_test_1", "object": "invoice", "customer": customer})
