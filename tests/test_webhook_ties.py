"""Stripe `created` has one-second resolution and delivery order is not guaranteed: two events of the same second are
applied in arrival order, except that a tie never undoes a cancel (RT-security-1 / RT-concurrency-1)."""
import logging

import pytest
from fastapi.testclient import TestClient

from app.main import app
from tests.stripe_signing import SECRET, payment_failed_event, subscription_event
from tests.test_webhooks import T, at, post, row

DELETED = "customer.subscription.deleted"


@pytest.fixture(autouse=True)
def webhook_env(monkeypatch):
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", SECRET)
    monkeypatch.delenv("GRACE_PERIOD_HOURS", raising=False)


@pytest.fixture
def client():
    return TestClient(app)


def edge_status(client, tenant="tenant-1") -> int:
    """What the real edge says to this tenant right now: 200 allowed, 402 denied."""
    return client.get(f"/v1/tenants/{tenant}/charges", headers={"X-Tenant-Id": tenant},
                      params={"start": "2026-11-01T00:00:00Z", "end": "2026-11-02T00:00:00Z"}).status_code


def cancel(client, how: str, event_id: str, created: int = T):
    if how == "deleted":
        return post(client, subscription_event(event_id, created, status="active", event_type=DELETED))
    return post(client, subscription_event(event_id, created, status="canceled"))


@pytest.mark.parametrize("how", ["deleted", "status canceled"])
def test_a_same_second_active_event_after_a_cancel_does_not_reactivate(client, conn, how):
    post(client, subscription_event("evt_1", T, status="active"))
    assert cancel(client, how, "evt_2").json() == {"status": "processed"} and edge_status(client) == 402
    resp = post(client, subscription_event("evt_3", T, status="active"))            # late delivery, the same second
    assert resp.json() == {"status": "stale"}
    assert (row(conn)["status"], edge_status(client)) == ("canceled", 402)


@pytest.mark.parametrize("status, event_type", [
    ("active", "customer.subscription.created"), ("trialing", "customer.subscription.updated"),
    ("past_due", "customer.subscription.updated")])
def test_no_same_second_non_cancel_event_gets_past_a_cancel(client, conn, status, event_type):
    cancel(client, "deleted", "evt_1")
    resp = post(client, subscription_event("evt_2", T, status=status, event_type=event_type))
    assert resp.json() == {"status": "stale"} and row(conn)["status"] == "canceled"


def test_the_reverse_order_also_ends_canceled(client, conn):
    post(client, subscription_event("evt_1", T, status="active"))
    assert cancel(client, "deleted", "evt_2").json() == {"status": "processed"}
    assert (row(conn)["status"], edge_status(client)) == ("canceled", 402)


def test_a_second_cancel_in_the_same_second_is_still_applied(client, conn):
    cancel(client, "status canceled", "evt_1")
    assert cancel(client, "deleted", "evt_2").json() == {"status": "processed"} and row(conn)["status"] == "canceled"


def test_a_strictly_newer_active_event_is_a_real_resubscribe_and_reactivates(client, conn):
    cancel(client, "deleted", "evt_1")
    resp = post(client, subscription_event("evt_2", T + 1, status="active"))
    assert resp.json() == {"status": "processed"}
    assert (row(conn)["status"], row(conn)["last_event_at"], edge_status(client)) == ("active", at(T + 1), 200)


def test_a_tie_that_is_not_after_a_cancel_still_applies_in_arrival_order(client, conn):
    post(client, subscription_event("evt_1", T, status="active"))
    resp = post(client, subscription_event("evt_2", T, status="past_due"))
    assert resp.json() == {"status": "processed"} and row(conn)["status"] == "past_due"


def test_a_same_second_payment_failure_after_a_cancel_does_not_reopen_access(client, conn):
    cancel(client, "deleted", "evt_1")
    resp = post(client, payment_failed_event("evt_2", T))
    assert resp.json() == {"status": "ignored"}
    r = row(conn)
    assert (r["status"], r["grace_until"], edge_status(client)) == ("canceled", None, 402)


def test_a_same_second_cancel_after_a_payment_failure_still_cancels(client, conn):
    post(client, subscription_event("evt_1", T, status="active"))
    post(client, payment_failed_event("evt_2", T))
    assert row(conn)["grace_until"] is not None
    assert cancel(client, "deleted", "evt_3").json() == {"status": "processed"}
    r = row(conn)
    assert (r["status"], r["grace_until"], edge_status(client)) == ("canceled", None, 402)


def test_the_tie_is_logged_and_the_event_is_recorded_so_its_redelivery_is_a_duplicate(client, conn, caplog):
    cancel(client, "deleted", "evt_1")
    with caplog.at_level(logging.INFO, logger="app.webhooks"):
        post(client, subscription_event("evt_2", T, status="active"))
    assert "tie" in caplog.text and "tenant='tenant-1'" in caplog.text and "evt_2" in caplog.text
    assert post(client, subscription_event("evt_2", T, status="active")).json() == {"status": "duplicate"}
