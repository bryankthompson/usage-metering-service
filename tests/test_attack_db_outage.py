"""A database that dies AFTER the edge check must be a 503 on every route, not a bare 500.

The connection is killed on the server side (pg_terminate_backend) at the first statement the handler runs, so the
edge has already admitted the request. The edge's own 503 is covered in tests/test_edge.py.
"""

import time
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app import db, ingest, pricing, webhooks
from app.main import app
from tests.stripe_signing import SECRET, sign, subscription_event

T0 = datetime(2026, 11, 1, 0, 0, tzinfo=timezone.utc)
HEADERS = {"X-Tenant-Id": "tenant-1"}


@pytest.fixture
def client(conn, monkeypatch):
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", SECRET)
    conn.execute("INSERT INTO entitlements (tenant_id, status) VALUES ('tenant-1', 'active')")
    conn.commit()
    return TestClient(app, raise_server_exceptions=False)


def kill_the_connection_then(original):
    """Wrap a handler-side function: terminate the backend of the connection it is given, then call it."""

    def wrapper(conn, *args, **kwargs):
        pid = conn.info.backend_pid
        with db.connect() as killer:
            killer.execute("SELECT pg_terminate_backend(%s)", (pid,))
            for _ in range(200):  # wait until the backend is really gone
                if not killer.execute("SELECT 1 FROM pg_stat_activity WHERE pid = %s", (pid,)).fetchone():
                    break
                time.sleep(0.01)
        return original(conn, *args, **kwargs)

    return wrapper


def test_ingest_with_the_connection_killed_mid_request_is_503_and_stores_nothing(client, conn, monkeypatch):
    monkeypatch.setattr(ingest, "lock_hour_shared", kill_the_connection_then(ingest.lock_hour_shared))
    body = {
        "tenant_id": "tenant-1",
        "idempotency_key": "k1",
        "model": "m1",
        "tokens_in": 1,
        "tokens_out": 1,
        "occurred_at": "2026-11-10T10:30:00Z",
    }
    resp = client.post("/v1/usage", json=body, headers=HEADERS)
    assert (resp.status_code, resp.json()) == (503, {"detail": "database unavailable"})
    assert conn.execute("SELECT count(*) AS n FROM usage_events").fetchone()["n"] == 0


def test_charges_with_the_connection_killed_mid_request_is_503(client, conn, monkeypatch):
    monkeypatch.setattr(pricing, "charge_lines", kill_the_connection_then(pricing.charge_lines))
    resp = client.get(
        "/v1/tenants/tenant-1/charges",
        params={"start": "2026-11-01T00:00:00Z", "end": "2026-11-02T00:00:00Z"},
        headers=HEADERS,
    )
    assert (resp.status_code, resp.json()) == (503, {"detail": "database unavailable"})


def test_webhook_with_the_connection_killed_mid_request_is_503_and_records_nothing(client, conn, monkeypatch):
    monkeypatch.setattr(webhooks, "handle_event", kill_the_connection_then(webhooks.handle_event))
    payload = subscription_event("evt_1", int(time.time()), tenant_id="tenant-2")
    resp = client.post("/v1/stripe/webhook", content=payload, headers={"Stripe-Signature": sign(payload)})
    assert (resp.status_code, resp.json()) == (503, {"detail": "database unavailable"})
    assert conn.execute("SELECT count(*) AS n FROM stripe_events").fetchone()["n"] == 0
    assert conn.execute("SELECT count(*) AS n FROM entitlements WHERE tenant_id = 'tenant-2'").fetchone()["n"] == 0


def test_the_api_recovers_on_the_next_request_after_a_killed_connection(client, conn, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(pricing, "charge_lines", kill_the_connection_then(pricing.charge_lines))
        assert (
            client.get(
                "/v1/tenants/tenant-1/charges",
                params={"start": "2026-11-01T00:00:00Z", "end": "2026-11-02T00:00:00Z"},
                headers=HEADERS,
            ).status_code
            == 503
        )
    assert (
        client.get(
            "/v1/tenants/tenant-1/charges",
            params={"start": "2026-11-01T00:00:00Z", "end": "2026-11-02T00:00:00Z"},
            headers=HEADERS,
        ).status_code
        == 200
    )


def test_a_database_that_is_unreachable_at_connect_time_is_503_on_the_webhook(client, conn, monkeypatch):
    # The signature is checked first and needs no database; the failure comes when the handler opens its connection.
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql://metering:metering@localhost:1/metering"
    )  # nothing listens on port 1
    payload = subscription_event("evt_2", int(time.time()), tenant_id="tenant-2")
    resp = client.post("/v1/stripe/webhook", content=payload, headers={"Stripe-Signature": sign(payload)})
    assert (resp.status_code, resp.json()) == (503, {"detail": "database unavailable"})
    bad = client.post("/v1/stripe/webhook", content=payload, headers={"Stripe-Signature": "t=1,v1=00"})
    assert bad.status_code == 400  # an unsigned request never reaches the database
