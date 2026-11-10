import json
import logging
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.main import app
from tests.stripe_signing import SECRET, event, payment_failed_event, sign, subscription_event

T = 1_793_000_000                                    # a fixed event time, in unix seconds
URL = "/v1/stripe/webhook"


def at(seconds: int) -> datetime:
    return datetime.fromtimestamp(seconds, tz=timezone.utc)


@pytest.fixture(autouse=True)
def webhook_env(monkeypatch):
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", SECRET)
    monkeypatch.delenv("GRACE_PERIOD_HOURS", raising=False)


@pytest.fixture
def client():
    return TestClient(app)


def post(client, payload: bytes, signature: str | None = None):
    headers = {"Stripe-Signature": signature if signature is not None else sign(payload)}
    return client.post(URL, content=payload, headers=headers)


def row(conn, tenant="tenant-1"):
    return conn.execute("SELECT * FROM entitlements WHERE tenant_id = %s", (tenant,)).fetchone()


def count(conn, table):
    return conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"]


# --- signature and secret: nothing unsigned is ever accepted, and nothing changes in the database ---

def test_bad_signature_is_400_and_changes_nothing(client, conn):
    payload = subscription_event("evt_1", T)
    resp = post(client, payload, sign(payload, secret="whsec_test_some_other_secret"))
    assert resp.status_code == 400
    assert count(conn, "entitlements") == 0 and count(conn, "stripe_events") == 0


def test_tampered_payload_is_400(client, conn):
    payload = subscription_event("evt_1", T)
    resp = post(client, payload.replace(b"active", b"canceled"), sign(payload))
    assert resp.status_code == 400 and count(conn, "entitlements") == 0


def test_missing_signature_header_is_400(client, conn):
    resp = client.post(URL, content=subscription_event("evt_1", T))
    assert resp.status_code == 400 and count(conn, "entitlements") == 0


def test_replayed_old_timestamp_is_400(client, conn):
    payload = subscription_event("evt_1", T)
    resp = post(client, payload, sign(payload, timestamp=int(time.time()) - 3600))
    assert resp.status_code == 400 and count(conn, "entitlements") == 0


def test_missing_secret_setting_is_503(client, conn, monkeypatch):
    monkeypatch.delenv("STRIPE_WEBHOOK_SECRET")
    payload = subscription_event("evt_1", T)
    resp = post(client, payload)
    assert resp.status_code == 503 and count(conn, "entitlements") == 0


def test_signed_but_malformed_json_is_400(client, conn):
    resp = post(client, b"not json at all")
    assert resp.status_code == 400 and count(conn, "stripe_events") == 0


# --- customer.subscription.* ---

def test_subscription_active_creates_the_entitlement(client, conn):
    resp = post(client, subscription_event("evt_1", T))
    assert resp.status_code == 200 and resp.json() == {"status": "processed"}
    r = row(conn)
    assert (r["status"], r["plan"], r["stripe_customer_id"], r["stripe_subscription_id"]) == (
        "active", "price_test_pro", "cus_test_1", "sub_test_1")
    assert r["last_event_at"] == at(T) and r["grace_until"] is None
    assert count(conn, "stripe_events") == 1


def test_trialing_counts_as_active(client, conn):
    post(client, subscription_event("evt_1", T, status="trialing"))
    assert row(conn)["status"] == "active"


@pytest.mark.parametrize("stripe_status", ["canceled", "unpaid", "incomplete", "incomplete_expired", "paused", "brand_new"])
def test_every_other_status_denies(client, conn, stripe_status):
    post(client, subscription_event("evt_1", T, status="active"))
    post(client, subscription_event("evt_2", T + 10, status=stripe_status))
    assert row(conn)["status"] == "canceled"


def test_subscription_changes_the_plan(client, conn):
    post(client, subscription_event("evt_1", T, price="price_test_basic"))
    post(client, subscription_event("evt_2", T + 10, price="price_test_pro"))
    assert row(conn)["plan"] == "price_test_pro"


def test_subscription_created_and_deleted_use_the_same_mapping(client, conn):
    post(client, subscription_event("evt_1", T, event_type="customer.subscription.created"))
    assert row(conn)["status"] == "active"
    post(client, subscription_event("evt_2", T + 10, status="canceled", event_type="customer.subscription.deleted"))
    assert row(conn)["status"] == "canceled"


def test_subscription_without_tenant_id_is_ignored(client, conn):
    resp = post(client, subscription_event("evt_1", T, tenant_id=None))
    assert resp.json() == {"status": "ignored"}
    assert count(conn, "entitlements") == 0 and count(conn, "stripe_events") == 0


# --- invoice.payment_failed ---

def test_payment_failed_sets_past_due_and_a_72_hour_grace(client, conn):
    post(client, subscription_event("evt_1", T))
    resp = post(client, payment_failed_event("evt_2", T + 100))
    assert resp.json() == {"status": "processed"}
    r = row(conn)
    assert r["status"] == "past_due"
    assert r["payment_failed_at"] == at(T + 100)
    assert r["grace_until"] == at(T + 100) + timedelta(hours=72)


def test_grace_period_hours_comes_from_the_environment(client, conn, monkeypatch):
    monkeypatch.setenv("GRACE_PERIOD_HOURS", "24")
    post(client, subscription_event("evt_1", T))
    post(client, payment_failed_event("evt_2", T + 100))
    assert row(conn)["grace_until"] == at(T + 100) + timedelta(hours=24)


def test_subscription_past_due_keeps_the_existing_grace_window(client, conn):
    post(client, subscription_event("evt_1", T))
    post(client, payment_failed_event("evt_2", T + 100))
    before = row(conn)
    post(client, subscription_event("evt_3", T + 200, status="past_due", price="price_test_basic"))
    after = row(conn)
    assert after["status"] == "past_due"
    assert (after["payment_failed_at"], after["grace_until"]) == (before["payment_failed_at"], before["grace_until"])
    assert after["plan"] == "price_test_basic"


def test_subscription_past_due_with_no_grace_window_has_none(client, conn):
    post(client, subscription_event("evt_1", T, status="past_due"))
    r = row(conn)
    assert r["status"] == "past_due" and r["grace_until"] is None          # the edge denies this (see test_edge)


def test_active_again_clears_the_failure_and_the_grace(client, conn):
    post(client, subscription_event("evt_1", T))
    post(client, payment_failed_event("evt_2", T + 100))
    post(client, subscription_event("evt_3", T + 200, status="active"))
    r = row(conn)
    assert (r["status"], r["payment_failed_at"], r["grace_until"]) == ("active", None, None)


def test_payment_failed_for_an_unknown_customer_is_ignored_and_logged(client, conn, caplog):
    with caplog.at_level(logging.INFO, logger="app.webhooks"):
        resp = post(client, payment_failed_event("evt_1", T, customer="cus_nobody"))
    assert resp.status_code == 200 and resp.json() == {"status": "ignored"}
    assert count(conn, "entitlements") == 0 and count(conn, "stripe_events") == 0
    assert "webhook ignored: event=evt_1" in caplog.text


def test_payment_failed_does_not_reopen_a_canceled_tenant(client, conn):
    post(client, subscription_event("evt_1", T, status="canceled"))
    resp = post(client, payment_failed_event("evt_2", T + 100))
    assert resp.json() == {"status": "ignored"}
    r = row(conn)
    assert (r["status"], r["grace_until"]) == ("canceled", None)


def test_unhandled_event_type_is_ignored(client, conn):
    resp = post(client, event("evt_1", "customer.created", T, {"id": "cus_test_1"}))
    assert resp.status_code == 200 and resp.json() == {"status": "ignored"}
    assert count(conn, "stripe_events") == 0


# --- once per event id, and in order ---

def test_duplicate_delivery_changes_nothing(client, conn):
    first = payment_failed_event("evt_2", T + 100)
    post(client, subscription_event("evt_1", T))
    assert post(client, first).json() == {"status": "processed"}
    post(client, subscription_event("evt_3", T + 200, status="active"))       # newer state: active again
    resp = post(client, first)                                                # Stripe redelivers evt_2
    assert resp.status_code == 200 and resp.json() == {"status": "duplicate"}
    assert row(conn)["status"] == "active"
    assert count(conn, "stripe_events") == 3


def test_older_event_does_not_overwrite_newer_state(client, conn):
    post(client, subscription_event("evt_new", T + 500, status="active"))
    resp = post(client, subscription_event("evt_old", T + 100, status="canceled"))
    assert resp.json() == {"status": "stale"}
    r = row(conn)
    assert (r["status"], r["last_event_at"]) == ("active", at(T + 500))
    assert post(client, subscription_event("evt_old", T + 100, status="canceled")).json() == {"status": "duplicate"}


def test_stale_payment_failure_does_not_downgrade_a_newer_active(client, conn):
    post(client, subscription_event("evt_1", T + 500))
    resp = post(client, payment_failed_event("evt_2", T + 100))
    assert resp.json() == {"status": "stale"}
    assert row(conn)["status"] == "active"


def test_events_in_the_same_second_apply_in_arrival_order(client, conn):
    post(client, subscription_event("evt_1", T, status="active"))
    resp = post(client, payment_failed_event("evt_2", T))
    assert resp.json() == {"status": "processed"} and row(conn)["status"] == "past_due"


def test_a_failure_while_applying_leaves_no_event_record(conn, monkeypatch):
    # The event id and the entitlement change commit together: if applying raises, a Stripe retry starts clean.
    monkeypatch.setenv("GRACE_PERIOD_HOURS", "not-a-number")
    safe_client = TestClient(app, raise_server_exceptions=False)
    post(safe_client, subscription_event("evt_1", T))
    resp = post(safe_client, payment_failed_event("evt_2", T + 100))
    assert resp.status_code == 503
    assert count(conn, "stripe_events") == 1 and row(conn)["status"] == "active"


def test_a_signed_event_missing_fields_is_a_400(client):
    resp = post(client, json.dumps({"id": "evt_bad", "type": "customer.subscription.updated", "created": T}).encode())
    assert resp.status_code == 400


def test_an_older_invoice_failure_fills_a_missing_grace_window(client, conn):
    post(client, subscription_event("evt_1", T + 100, status="past_due"))
    resp = post(client, payment_failed_event("evt_2", T))
    assert resp.json() == {"status": "processed"}
    r = row(conn)
    # The window is counted from the failure's own time, and last_event_at stays at the newer event.
    assert (r["payment_failed_at"], r["grace_until"], r["last_event_at"]) == (at(T), at(T) + timedelta(hours=72), at(T + 100))


def test_a_repeated_payment_failure_does_not_extend_the_grace_window(client, conn):
    post(client, subscription_event("evt_1", T))
    post(client, payment_failed_event("evt_2", T + 100))
    resp = post(client, payment_failed_event("evt_3", T + 86400))             # a Stripe retry a day later
    assert resp.json() == {"status": "processed"}
    r = row(conn)
    assert (r["status"], r["payment_failed_at"], r["grace_until"]) == ("past_due", at(T + 100), at(T + 100) + timedelta(hours=72))
    assert r["last_event_at"] == at(T + 86400)                                # the retry is still recorded as the newest event


def test_a_new_delinquency_after_active_again_gets_a_fresh_window(client, conn):
    post(client, subscription_event("evt_1", T))
    post(client, payment_failed_event("evt_2", T + 100))
    post(client, subscription_event("evt_3", T + 200, status="active"))       # cured: clears the window
    post(client, payment_failed_event("evt_4", T + 300))
    r = row(conn)
    assert (r["payment_failed_at"], r["grace_until"]) == (at(T + 300), at(T + 300) + timedelta(hours=72))


def test_subscription_deleted_cancels_whatever_status_the_payload_carries(client, conn):
    post(client, subscription_event("evt_1", T))
    resp = post(client, subscription_event("evt_2", T + 10, status="active", event_type="customer.subscription.deleted"))
    assert resp.json() == {"status": "processed"} and row(conn)["status"] == "canceled"


def test_deleting_a_replaced_subscription_does_not_cancel_the_current_one(client, conn):
    post(client, subscription_event("evt_1", T, sub_id="sub_new"))
    resp = post(client, subscription_event("evt_2", T + 10, status="canceled", sub_id="sub_old",
                                           event_type="customer.subscription.deleted"))
    assert resp.json() == {"status": "stale"}
    assert row(conn)["status"] == "active" and row(conn)["stripe_subscription_id"] == "sub_new"


def test_an_older_failure_after_a_newer_past_due_opens_one_window_and_a_later_older_one_does_not_move_it(client, conn):
    post(client, subscription_event("evt_1", T + 200, status="active"))
    post(client, subscription_event("evt_2", T + 400, status="past_due"))     # past_due, no window yet
    assert post(client, payment_failed_event("evt_3", T)).json() == {"status": "processed"}   # opens it, from T
    assert post(client, payment_failed_event("evt_4", T + 300)).json() == {"status": "stale"}  # window already open
    r = row(conn)
    assert (r["payment_failed_at"], r["grace_until"], r["last_event_at"]) == (at(T), at(T) + timedelta(hours=72), at(T + 400))
