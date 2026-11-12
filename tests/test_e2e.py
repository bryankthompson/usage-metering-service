"""One story through the real app: no dependency overrides, the real edge, the real database.

price table -> refused without an entitlement -> signed Stripe event -> usage -> rollup -> charges
-> a late event -> a failed payment, the grace window and recovery.
"""
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import edge
from app.main import app
from app.pricing import add_price_version
from app.rollup import close_hour
from tests.stripe_signing import SECRET, payment_failed_event, sign, subscription_event

TENANT = "tenant-1"
HEADERS = {"X-Tenant-Id": TENANT}


def utc(text: str) -> datetime:
    return datetime.fromisoformat(text)


def unix(text: str) -> int:
    return int(utc(text).timestamp())


@pytest.fixture
def clock(monkeypatch):
    """The edge's clock, held still and moved by the test (no sleeping)."""
    now = {"value": utc("2026-11-10T12:00:00+00:00")}
    monkeypatch.setattr(edge, "now", lambda: now["value"])
    return now


@pytest.fixture
def client(conn, monkeypatch, clock):
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", SECRET)
    monkeypatch.delenv("GRACE_PERIOD_HOURS", raising=False)
    assert app.dependency_overrides == {}, "an override left by another test would hide the real edge"
    return TestClient(app)


def usage(key, model, tokens_in, tokens_out, occurred_at):
    return {"tenant_id": TENANT, "idempotency_key": key, "model": model, "tokens_in": tokens_in,
            "tokens_out": tokens_out, "occurred_at": occurred_at}


def count(conn, table):
    return conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"]


def entitlement(conn):
    return conn.execute("SELECT * FROM entitlements WHERE tenant_id = %s", (TENANT,)).fetchone()


def webhook(client, payload: bytes):
    return client.post("/v1/stripe/webhook", content=payload, headers={"Stripe-Signature": sign(payload)})


def charges(client, start, end):
    return client.get(f"/v1/tenants/{TENANT}/charges", params={"start": start, "end": end}, headers=HEADERS)


def test_the_whole_path_through_the_real_app(client, conn, clock):
    h1, h2 = utc("2026-11-10T10:00:00+00:00"), utc("2026-11-10T11:00:00+00:00")

    # 1. Price table. model-a changes price at 11:00 UTC (version 2); model-b has one version.
    assert add_price_version(conn, "model-a", utc("2026-11-01T00:00:00+00:00"), 2000, 4000) == 1
    assert add_price_version(conn, "model-a", h2, 3000, 6000) == 2
    assert add_price_version(conn, "model-b", utc("2026-11-01T00:00:00+00:00"), 1500, 2500) == 1
    assert count(conn, "price_versions") == 3

    # 2. Before any entitlement the edge refuses, and nothing is stored.
    event = usage("k1", "model-a", 1000, 500, "2026-11-10T10:05:00Z")
    assert client.post("/v1/usage", json=event, headers=HEADERS).status_code == 402   # unknown tenant
    assert client.post("/v1/usage", json=event).status_code == 401                     # no X-Tenant-Id header
    assert count(conn, "usage_events") == 0

    # 3. A forged event changes nothing; a signed customer.subscription.updated makes the tenant active.
    forged = subscription_event("evt_forged", unix("2026-11-10T09:00:00+00:00"), tenant_id=TENANT)
    resp = client.post("/v1/stripe/webhook", content=forged, headers={"Stripe-Signature": sign(forged, secret="whsec_test_wrong")})
    assert resp.status_code == 400 and count(conn, "entitlements") == 0
    resp = webhook(client, subscription_event("evt_sub_1", unix("2026-11-10T09:00:00+00:00"), tenant_id=TENANT))
    assert resp.status_code == 200 and resp.json() == {"status": "processed"}
    row = entitlement(conn)
    assert (row["status"], row["grace_until"]) == ("active", None) and count(conn, "stripe_events") == 1

    # 4. Usage through the real edge: three events in hour 10:00 (one resent, one for another model), one at 11:15.
    assert client.post("/v1/usage", json=event, headers=HEADERS).status_code == 201
    assert client.post("/v1/usage", json=usage("k2", "model-a", 2000, 100, "2026-11-10T10:40:00Z"), headers=HEADERS).status_code == 201
    resend = client.post("/v1/usage", json=event, headers=HEADERS)
    assert resend.status_code == 200 and resend.json()["status"] == "duplicate"
    assert client.post("/v1/usage", json=usage("k3", "model-b", 501, 1500, "2026-11-10T10:50:00Z"), headers=HEADERS).status_code == 201
    assert client.post("/v1/usage", json=usage("k4", "model-a", 1000, 1000, "2026-11-10T11:15:00Z"), headers=HEADERS).status_code == 201
    assert count(conn, "usage_events") == 4                                            # the resend was counted once
    assert conn.execute("SELECT count(*) AS n FROM usage_events WHERE late").fetchone()["n"] == 0

    # 5. Close both hours; the rollup rows are the sums per model.
    assert close_hour(conn, h1) == (2, 3) and close_hour(conn, h2) == (1, 1)             # (rows written, events counted)
    rollups = [(r["hour_start"], r["model"], r["tokens_in"], r["tokens_out"], r["event_count"]) for r in conn.execute(
        "SELECT * FROM usage_rollups_hourly ORDER BY hour_start, model")]
    assert rollups == [
        (h1, "model-a", 3000, 600, 2),      # k1 + k2: 1000 + 2000 in, 500 + 100 out
        (h1, "model-b", 501, 1500, 1),      # k3
        (h2, "model-a", 1000, 1000, 1),     # k4
    ]
    assert count(conn, "closed_hours") == 2

    # 6. Charges, each rollup row rated at the price in force at its own hour, rounded half up once per row:
    #    h1 model-a, version 1 (2000 in, 4000 out per 1k): (3000*2000 + 600*4000) / 1000 = 8,400,000 / 1000 = 8400
    #    h1 model-b, version 1 (1500 in, 2500 out per 1k): (501*1500 + 1500*2500) / 1000 = 4,501,500 / 1000 = 4501.5 -> 4502
    #    h2 model-a, version 2 (3000 in, 6000 out per 1k): (1000*3000 + 1000*6000) / 1000 = 9,000,000 / 1000 = 9000
    #    total = 8400 + 4502 + 9000 = 21902
    resp = charges(client, "2026-11-10T10:00:00Z", "2026-11-10T12:00:00Z")
    assert resp.status_code == 200
    body = resp.json()
    assert [(ln["model"], ln["price_version"], ln["micros"]) for ln in body["lines"]] == [
        ("model-a", 1, 8400), ("model-b", 1, 4502), ("model-a", 2, 9000)]
    assert body["total_micros"] == 21902 and isinstance(body["total_micros"], int)

    # 7. A late event for the closed hour 10:00 is accepted and stored late; the rollup and the total do not move.
    late = client.post("/v1/usage", json=usage("k5", "model-a", 7000, 7000, "2026-11-10T10:55:00Z"), headers=HEADERS)
    assert late.status_code == 201 and late.json()["late"] is True                     # the response says it is late
    assert conn.execute("SELECT late FROM usage_events WHERE idempotency_key = 'k5'").fetchone()["late"] is True
    assert count(conn, "usage_events") == 5 and count(conn, "usage_rollups_hourly") == 3
    assert conn.execute("SELECT tokens_in, event_count FROM usage_rollups_hourly WHERE hour_start = %s AND model = 'model-a'",
                        (h1,)).fetchone() == {"tokens_in": 3000, "event_count": 2}
    assert charges(client, "2026-11-10T10:00:00Z", "2026-11-10T12:00:00Z").json()["total_micros"] == 21902

    # 8. A failed payment opens a 72 hour grace window: ingest still works inside it, is refused after it, and a
    #    later signed active subscription event lets it through again.
    failed_at = unix("2026-11-10T12:00:00+00:00")
    assert webhook(client, payment_failed_event("evt_fail_1", failed_at)).json() == {"status": "processed"}
    grace_until = utc("2026-11-13T12:00:00+00:00")                                      # failed_at + 72 hours
    row = entitlement(conn)
    assert (row["status"], row["grace_until"]) == ("past_due", grace_until)

    clock["value"] = grace_until                                                         # the last allowed instant
    assert client.post("/v1/usage", json=usage("k6", "model-a", 10, 10, "2026-11-13T12:00:00Z"), headers=HEADERS).status_code == 201
    stored = count(conn, "usage_events")
    assert stored == 6

    clock["value"] = grace_until + timedelta(seconds=1)                                  # one second past the window
    refused = client.post("/v1/usage", json=usage("k7", "model-a", 10, 10, "2026-11-13T12:00:01Z"), headers=HEADERS)
    assert refused.status_code == 402
    assert charges(client, "2026-11-10T10:00:00Z", "2026-11-10T12:00:00Z").status_code == 402   # the whole edge is closed
    assert count(conn, "usage_events") == stored                                         # a refused event is not stored

    recovered = subscription_event("evt_sub_2", unix("2026-11-13T12:05:00+00:00"), tenant_id=TENANT, status="active")
    assert webhook(client, recovered).json() == {"status": "processed"}
    row = entitlement(conn)
    assert (row["status"], row["payment_failed_at"], row["grace_until"]) == ("active", None, None)
    assert client.post("/v1/usage", json=usage("k7", "model-a", 10, 10, "2026-11-13T12:00:01Z"), headers=HEADERS).status_code == 201
    assert count(conn, "usage_events") == stored + 1
