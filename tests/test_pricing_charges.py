from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.edge import require_entitlement
from app.main import app
from app.pricing import add_price_version
from app.rollup import close_hour

T0 = datetime(2026, 11, 1, 0, 0, tzinfo=timezone.utc)
RANGE = {"start": "2026-11-01T00:00:00+00:00", "end": "2026-11-01T03:00:00+00:00"}


@pytest.fixture
def client():
    app.dependency_overrides[require_entitlement] = lambda: "tenant-1"
    yield TestClient(app)
    app.dependency_overrides.clear()


def seed(conn):
    add_price_version(conn, "m1", T0, 1000, 2000)
    conn.execute(
        "INSERT INTO usage_rollups_hourly (tenant_id, model, hour_start, tokens_in, tokens_out, event_count)"
        " VALUES ('tenant-1', 'm1', %s, 1000, 500, 3), ('tenant-1', 'm1', %s, 1, 0, 1)",
        (T0, T0 + timedelta(hours=1)))
    conn.commit()


def test_charges_response(client, conn):
    seed(conn)
    resp = client.get("/v1/tenants/tenant-1/charges", params=RANGE)
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_micros"] == 2000 + 1                                      # 1000 + 1000 for hour 0, 1 for hour 1
    assert [line["micros"] for line in body["lines"]] == [2000, 1]
    assert body["lines"][0]["price_version"] == 1
    assert isinstance(body["total_micros"], int)


def test_charges_for_another_tenant_is_403(client, conn):
    seed(conn)
    assert client.get("/v1/tenants/tenant-2/charges", params=RANGE).status_code == 403


def test_charges_need_timezone_aware_datetimes(client, conn):
    naive = {"start": "2026-11-01T00:00:00", "end": "2026-11-01T03:00:00"}
    assert client.get("/v1/tenants/tenant-1/charges", params=naive).status_code == 422


def test_charges_range_must_run_forward(client, conn):
    backwards = {"start": RANGE["end"], "end": RANGE["start"]}
    assert client.get("/v1/tenants/tenant-1/charges", params=backwards).status_code == 422


def test_charges_with_a_missing_price_is_409_not_zero(client, conn):
    conn.execute(
        "INSERT INTO usage_rollups_hourly (tenant_id, model, hour_start, tokens_in, tokens_out, event_count)"
        " VALUES ('tenant-1', 'unpriced', %s, 10, 10, 1)", (T0,))
    conn.commit()
    resp = client.get("/v1/tenants/tenant-1/charges", params=RANGE)
    assert resp.status_code == 409
    assert "unpriced" in resp.json()["detail"]


def test_charges_are_behind_the_edge(conn):
    # No override: the real edge decides. Whatever it answers, it must not be a 200 with charges.
    seed(conn)
    resp = TestClient(app).get("/v1/tenants/tenant-1/charges", params=RANGE)
    assert resp.status_code in (401, 402, 503)


@pytest.mark.parametrize("split", ["2026-11-01T10:30:00Z", "2026-11-01T11:00:00Z"])
def test_a_range_split_inside_or_on_an_hour_bills_no_hour_twice_and_none_dropped(client, conn, split):
    """An hour belongs to the range its START falls in (`start <= hour_start < end`, docs/billing.md), so
    charges(a, b) + charges(b, c) == charges(a, c) for any b: mid-hour (10:30) and exactly on the hour (11:00)."""
    add_price_version(conn, "m1", T0, 1000, 2000)
    add_price_version(conn, "m2", T0, 1500, 3000)
    sent = [("m1", "10:05", 1000, 500), ("m2", "10:40", 200, 100), ("m1", "11:10", 1, 0), ("m2", "11:55", 3, 3)]
    for i, (model, at, tokens_in, tokens_out) in enumerate(sent):
        r = client.post("/v1/usage", json={"tenant_id": "tenant-1", "idempotency_key": f"k{i}", "model": model,
                                           "tokens_in": tokens_in, "tokens_out": tokens_out,
                                           "occurred_at": f"2026-11-01T{at}:00Z"})
        assert r.status_code == 201
    for hour in (10, 11):
        assert close_hour(conn, T0 + timedelta(hours=hour)) is not None

    def charges(start, end):
        r = client.get("/v1/tenants/tenant-1/charges", params={"start": start, "end": end})
        assert r.status_code == 200
        assert r.json()["unclosed_hours"] == []
        return r.json()

    whole = charges("2026-11-01T10:00:00Z", "2026-11-01T12:00:00Z")
    first = charges("2026-11-01T10:00:00Z", split)
    second = charges(split, "2026-11-01T12:00:00Z")
    assert whole["total_micros"] == 2000 + 600 + 1 + 14                          # all four lines, once each
    assert len(whole["lines"]) == 4
    assert first["total_micros"] + second["total_micros"] == whole["total_micros"]
    assert first["lines"] + second["lines"] == whole["lines"]                    # the same lines, none twice, none dropped
    assert {line["hour_start"] for line in first["lines"]} == {"2026-11-01T10:00:00Z"}   # the hour that starts before the split
    assert {line["hour_start"] for line in second["lines"]} == {"2026-11-01T11:00:00Z"}  # the hour that starts at or after it
