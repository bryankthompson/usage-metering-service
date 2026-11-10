from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.edge import require_entitlement
from app.main import app
from app.pricing import add_price_version

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
