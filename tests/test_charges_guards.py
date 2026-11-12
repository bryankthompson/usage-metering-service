"""Two guards on GET /v1/tenants/{id}/charges: a range wider than 366 days is a 422 (RT-operations-5: eight wide calls took
the API to 1.7 GiB), and the response lists the hours in range that hold usage but are not closed yet, so a total that is
partial because the rollup is behind or stuck says so (RT-operations-2, RT-money-6)."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.edge import require_entitlement
from app.main import app
from app.pricing import add_price_version
from app.rollup import close_hour

T0 = datetime(2026, 11, 1, 0, 0, tzinfo=timezone.utc)
HOUR = timedelta(hours=1)


@pytest.fixture
def client():
    app.dependency_overrides[require_entitlement] = lambda: "tenant-1"
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()


def charges(client, start=T0, end=T0 + 6 * HOUR):
    return client.get("/v1/tenants/tenant-1/charges", params={"start": start.isoformat(), "end": end.isoformat()})


def event(conn, hour: int, tenant="tenant-1", key=None, late=False, tokens=10, minute=30):
    conn.execute(
        "INSERT INTO usage_events (tenant_id, idempotency_key, model, tokens_in, tokens_out, occurred_at, late)"
        " VALUES (%s, %s, 'm1', %s, %s, %s, %s)",
        (
            tenant,
            key or f"{tenant}-{hour}-{minute}-{late}",
            tokens,
            tokens,
            T0 + hour * HOUR + timedelta(minutes=minute),
            late,
        ),
    )
    conn.commit()


def listed(response) -> list[datetime]:
    return [datetime.fromisoformat(h) for h in response.json()["unclosed_hours"]]


# --- the range cap ---------------------------------------------------------------------------------------------------


def test_a_range_of_exactly_366_days_is_answered_and_one_microsecond_more_is_422(client, conn):
    assert charges(client, T0, T0 + timedelta(days=366)).status_code == 200
    r = charges(client, T0, T0 + timedelta(days=366, microseconds=1))
    assert r.status_code == 422 and "366 days" in r.json()["detail"]


@pytest.mark.parametrize(
    "start, end",
    [
        ("0001-01-01T00:00:00Z", "9999-12-31T23:59:59Z"),
        ("2000-01-01T00:00:00Z", "2100-01-01T00:00:00Z"),
        ("2026-01-01T00:00:00Z", "2027-01-02T00:00:01Z"),
    ],
)
def test_wide_ranges_are_422_whatever_they_are_written_as(client, conn, start, end):
    assert client.get("/v1/tenants/tenant-1/charges", params={"start": start, "end": end}).status_code == 422


def test_the_width_is_measured_in_instants_not_in_the_offsets_they_are_written_with(client, conn):
    wide = client.get(
        "/v1/tenants/tenant-1/charges",
        params={"start": "2026-01-01T00:00:00+14:00", "end": "2027-01-02T00:00:00-12:00"},
    )
    assert wide.status_code == 422  # the dates are 366 days apart, the instants 367 days and 2 hours


# --- unclosed_hours --------------------------------------------------------------------------------------------------


def test_the_response_always_has_unclosed_hours_and_it_is_empty_when_nothing_is_waiting(client, conn):
    r = charges(client)
    assert r.status_code == 200 and r.json()["unclosed_hours"] == []


def test_hours_with_usage_and_no_closed_row_are_listed_sorted_and_a_closed_hour_is_not(client, conn):
    add_price_version(conn, "m1", T0, 1000, 1000)
    for hour in (2, 0, 1):
        event(conn, hour)
    assert close_hour(conn, T0) == (1, 1)
    r = charges(client)
    assert r.status_code == 200 and listed(r) == [T0 + HOUR, T0 + 2 * HOUR]
    assert len(r.json()["lines"]) == 1
    assert r.json()["total_micros"] == 20  # only the closed hour is billed


def test_late_events_do_not_make_an_hour_unclosed(client, conn):
    add_price_version(conn, "m1", T0, 1000, 1000)
    event(conn, 0)
    close_hour(conn, T0)
    event(conn, 0, late=True, minute=45)
    event(conn, 3, late=True)  # nothing but late events, no closed row
    assert listed(charges(client)) == []


def test_another_tenants_unclosed_hours_are_not_listed(client, conn):
    event(conn, 1, tenant="tenant-2")
    assert listed(charges(client)) == []


def test_only_hours_that_start_inside_the_range_are_listed(client, conn):
    for hour in (0, 1, 5, 6):
        event(conn, hour)
    # [start, end) = [01:30, 06:00): hour 0 starts before start, hour 1 starts before 01:30, hour 6 starts at end
    r = charges(client, T0 + timedelta(minutes=90), T0 + 6 * HOUR)
    assert listed(r) == [T0 + 5 * HOUR]
    assert listed(charges(client, T0 + HOUR, T0 + 6 * HOUR)) == [
        T0 + HOUR,
        T0 + 5 * HOUR,
    ]  # an hour starting AT start counts


def test_at_most_100_hours_are_listed_the_earliest_first(client, conn):
    conn.execute(
        "INSERT INTO usage_events (tenant_id, idempotency_key, model, tokens_in, tokens_out, occurred_at)"
        " SELECT 'tenant-1', 'k' || g, 'm1', 1, 1, %s + g * interval '1 hour' + interval '5 minutes' FROM generate_series(0, 119) g",
        (T0,),
    )
    conn.commit()
    r = charges(client, T0, T0 + timedelta(days=30))
    assert listed(r) == [T0 + h * HOUR for h in range(100)]


def test_an_earlier_hour_the_rollup_never_closed_is_named_while_a_later_closed_hour_is_not(client, conn):
    # A stuck rollup, however it got stuck (an error on that hour, a crashed loop): no closed_hours row for the earlier hour.
    add_price_version(conn, "m1", T0, 1000, 1000)
    event(conn, 0)
    event(conn, 1)
    assert close_hour(conn, T0 + HOUR) == (1, 1)  # a later hour closes; hour 0 is never touched
    r = charges(client)
    assert r.status_code == 200 and listed(r) == [T0]  # the stuck hour is named, the closed one is not
    assert len(r.json()["lines"]) == 1 and r.json()["total_micros"] == 20  # the bill is partial, and now says so
