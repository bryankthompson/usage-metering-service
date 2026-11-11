"""A `customer.subscription.deleted` for the subscription the row holds is terminal: a canceled subscription id is never
reopened, so a delete that is OLDER than an applied payment failure is still applied (RT-concurrency-4). It is applied
without moving `last_event_at` backwards. A delete of a different subscription id, or a row with no stored id, is judged by the
shared clock as before."""
import pytest
from fastapi.testclient import TestClient

from app import edge
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


def delete(client, event_id, created, sub_id="sub_x"):
    return post(client, subscription_event(event_id, created, status="canceled", event_type=DELETED, sub_id=sub_id))


def failed_before_the_cancel(client, conn):
    """active(sub_x, T-10), then a failure created AFTER the cancel's own second, applied first."""
    post(client, subscription_event("evt_1", T - 10, sub_id="sub_x"))
    assert post(client, payment_failed_event("evt_2", T + 1)).json() == {"status": "processed"}
    assert row(conn)["status"] == "past_due"


def test_a_delete_older_than_an_applied_failure_still_cancels_the_stored_subscription(client, conn):
    c = client
    failed_before_the_cancel(c, conn)
    assert delete(c, "evt_3", T).json() == {"status": "processed"}
    r = row(conn)
    assert (r["status"], r["payment_failed_at"], r["grace_until"], r["last_event_at"]) == ("canceled", None, None, at(T + 1))
    assert edge_status(c) == 402
    assert delete(c, "evt_3", T).json() == {"status": "duplicate"}


def test_the_clock_does_not_move_backwards_so_a_late_active_event_cannot_reopen_the_cancel(client, conn):
    c = client
    failed_before_the_cancel(c, conn)
    delete(c, "evt_3", T)
    # last_event_at stays T+1: an updated(active) created at T+1 is a tie with a canceled tenant (stale), and an older one is stale.
    assert post(c, subscription_event("evt_4", T + 1, sub_id="sub_x")).json() == {"status": "stale"}
    assert post(c, subscription_event("evt_5", T - 5, sub_id="sub_x")).json() == {"status": "stale"}
    assert (row(conn)["status"], row(conn)["last_event_at"], edge_status(c)) == ("canceled", at(T + 1), 402)


def test_a_delete_newer_than_the_clock_moves_the_clock_forward(client, conn):
    c = client
    failed_before_the_cancel(c, conn)
    assert delete(c, "evt_3", T + 9).json() == {"status": "processed"}
    r = row(conn)
    assert (r["status"], r["last_event_at"]) == ("canceled", at(T + 9))


def test_a_late_delete_of_an_earlier_subscription_does_not_cancel_the_new_one(client, conn):
    c = client
    post(c, subscription_event("evt_1", T - 20, sub_id="sub_1"))
    assert delete(c, "evt_2", T - 10, sub_id="sub_1").json() == {"status": "processed"}
    assert post(c, subscription_event("evt_3", T + 5, sub_id="sub_2", event_type="customer.subscription.created")).json() == {"status": "processed"}
    assert delete(c, "evt_4", T + 1, sub_id="sub_1").json() == {"status": "stale"}
    r = row(conn)
    assert (r["status"], r["stripe_subscription_id"], r["last_event_at"], edge_status(c)) == ("active", "sub_2", at(T + 5), 200)


def test_a_row_with_no_stored_subscription_id_is_judged_by_the_shared_clock(client, conn):
    c = client
    post(c, subscription_event("evt_1", T - 10, sub_id=None))
    assert row(conn)["stripe_subscription_id"] is None
    assert post(c, payment_failed_event("evt_2", T + 1)).json() == {"status": "processed"}
    assert delete(c, "evt_3", T).json() == {"status": "stale"}
    assert row(conn)["status"] == "past_due"


def test_an_older_active_event_after_the_failure_is_still_stale(client, conn):
    c = client
    failed_before_the_cancel(c, conn)
    assert post(c, subscription_event("evt_3", T, sub_id="sub_x")).json() == {"status": "stale"}
    r = row(conn)
    assert (r["status"], r["last_event_at"]) == ("past_due", at(T + 1))
