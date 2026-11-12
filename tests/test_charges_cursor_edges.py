"""The cursor at its edges: a range at the ends of what datetime can hold, the longest cursor the service issues, one too long.

Own file: the paging contract itself is in test_charges_paging.py.
"""
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from app import pricing
from app.edge import require_entitlement
from app.ingest import MAX_TEXT
from app.main import app
from tests.test_charges_paging import HOUR, walk

ASTRAL = "\U0001F600"                  # one character, a surrogate pair in a JSON escape: the longest spelling per character


@pytest.fixture
def client_for():
    def make(tenant):
        app.dependency_overrides[require_entitlement] = lambda: tenant
        return TestClient(app, raise_server_exceptions=False)
    yield make
    app.dependency_overrides.clear()


def seed(conn, tenant, model, first_hour, hours):
    """A price dated at the first hour (a day before would leave year 1), then one rollup row per hour."""
    conn.execute("INSERT INTO price_versions (model, version, effective_from, input_micros_per_1k, output_micros_per_1k)"
                 " VALUES (%s, 1, %s, 1000, 2000)", (model, first_hour))
    for h in range(hours):
        conn.execute("INSERT INTO usage_rollups_hourly (tenant_id, model, hour_start, tokens_in, tokens_out, event_count)"
                     " VALUES (%s, %s, %s, %s, 500, 1)", (tenant, model, first_hour + h * HOUR, 1000 + h))
    conn.commit()


def walked_and_added_up(client, tenant, params, expected_lines):
    lines, pages = walk(client, params, 1, path=f"/v1/tenants/{quote(tenant)}/charges")
    assert len(pages) == expected_lines and len(lines) == expected_lines
    assert {page["total_micros"] for page in pages} == {sum(line["micros"] for line in lines)}
    assert pages[-1]["line_count"] == expected_lines


# --- a range at the ends of datetime ----------------------------------------------------------------------------------------

LOW = datetime(1, 1, 1, tzinfo=timezone(timedelta(hours=5)))               # 0000-12-31 19:00 UTC: not a datetime in UTC
HIGH_OFFSET = timezone(timedelta(hours=-5))
HIGH_END = datetime(9999, 12, 31, 23, 59, 59, tzinfo=HIGH_OFFSET)          # 10000-01-01 04:59:59 UTC: not a datetime in UTC


@pytest.mark.parametrize("start, end, first_hour", [
    (LOW, LOW + timedelta(hours=12), datetime(1, 1, 1, tzinfo=timezone.utc)),
    (HIGH_END - timedelta(days=2), HIGH_END, datetime(9999, 12, 30, 12, tzinfo=timezone.utc)),
], ids=["near year 1", "near year 9999"])
def test_a_range_at_the_end_of_what_datetime_holds_pages_and_adds_up(client_for, conn, start, end, first_hour):
    seed(conn, "tenant-1", "m1", first_hour, 3)
    params = {"start": start.isoformat(), "end": end.isoformat()}
    first = client_for("tenant-1").get("/v1/tenants/tenant-1/charges", params={**params, "limit": 1})
    assert first.status_code == 200, first.text
    assert first.json()["next_cursor"]
    walked_and_added_up(client_for("tenant-1"), "tenant-1", params, 3)


def test_the_same_instant_in_another_offset_is_accepted_at_the_end_of_datetime(client_for, conn):
    seed(conn, "tenant-1", "m1", datetime(9999, 12, 30, 12, tzinfo=timezone.utc), 3)
    client = client_for("tenant-1")
    start = HIGH_END - timedelta(days=2)
    first = client.get("/v1/tenants/tenant-1/charges", params={"start": start.isoformat(), "end": HIGH_END.isoformat(), "limit": 1})
    other = timezone(timedelta(hours=-6))                    # spelled by hand: astimezone goes through UTC and overflows
    same_start, same_end = start.replace(hour=22, tzinfo=other), HIGH_END.replace(hour=22, tzinfo=other)
    assert same_start == start and same_end == HIGH_END
    resp = client.get("/v1/tenants/tenant-1/charges", params={
        "start": same_start.isoformat(), "end": same_end.isoformat(),
        "limit": 1, "cursor": first.json()["next_cursor"]})
    assert resp.status_code == 200 and resp.json()["lines"][0]["hour_start"] == "9999-12-30T13:00:00Z"


def test_another_range_at_the_end_of_datetime_is_still_a_foreign_cursor(client_for, conn):
    seed(conn, "tenant-1", "m1", datetime(9999, 12, 30, 12, tzinfo=timezone.utc), 3)
    client = client_for("tenant-1")
    start = HIGH_END - timedelta(days=2)
    first = client.get("/v1/tenants/tenant-1/charges", params={"start": start.isoformat(), "end": HIGH_END.isoformat(), "limit": 1})
    resp = client.get("/v1/tenants/tenant-1/charges", params={
        "start": start.isoformat(), "end": (HIGH_END - HOUR).isoformat(), "limit": 1, "cursor": first.json()["next_cursor"]})
    assert resp.status_code == 422 and "foreign cursor" in resp.json()["detail"]


# --- the longest cursor the service issues ----------------------------------------------------------------------------------

def test_the_longest_cursor_the_service_issues_is_accepted_by_the_next_request(client_for, conn):
    tenant = model = ASTRAL * MAX_TEXT
    start = datetime(2026, 11, 1, 0, 0, 0, 123456, tzinfo=timezone(timedelta(hours=-11, minutes=-59)))       # the longest spelling
    seed(conn, tenant, model, datetime(2026, 11, 1, 12, tzinfo=timezone.utc), 3)
    params = {"start": start.isoformat(), "end": (start + timedelta(days=2)).isoformat()}
    path = f"/v1/tenants/{quote(tenant)}/charges"
    client = client_for(tenant)
    first = client.get(path, params={**params, "limit": 1})
    assert first.status_code == 200, first.text
    cursor = first.json()["next_cursor"]
    assert 0.5 * pricing.CURSOR_MAX_LENGTH < len(cursor) <= pricing.CURSOR_MAX_LENGTH, len(cursor)       # it is the worst case
    resp = client.get(path, params={**params, "limit": 1, "cursor": cursor})
    assert resp.status_code == 200 and resp.json()["lines"][0]["model"] == model
    walked_and_added_up(client, tenant, params, 3)


def test_a_cursor_over_the_length_cap_is_422_for_its_length(client_for, conn):
    seed(conn, "tenant-1", "m1", datetime(2026, 11, 1, tzinfo=timezone.utc), 3)
    params = {"start": "2026-11-01T00:00:00Z", "end": "2026-11-02T00:00:00Z", "limit": 1}
    client = client_for("tenant-1")
    at_cap = client.get("/v1/tenants/tenant-1/charges", params={**params, "cursor": "A" * 8192})
    over = client.get("/v1/tenants/tenant-1/charges", params={**params, "cursor": "A" * 8193})
    assert at_cap.status_code == 422 and "malformed" in at_cap.json()["detail"]            # read, and refused as garbage
    assert over.status_code == 422 and "at most" in str(over.json()["detail"])             # refused for its length, unread
