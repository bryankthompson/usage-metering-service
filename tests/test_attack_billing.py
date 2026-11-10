"""Adversarial tests for the edge, the price table and the charges route, run through the real app and the real edge.

Each test states the attack. Tests that only repeat what tests/test_edge.py, test_pricing.py or test_pricing_charges.py
already pin are not here.
"""
import time
from datetime import datetime, timedelta, timezone

import psycopg
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.edge import require_entitlement
from app.main import app
from app.pricing import NoPrice, add_price_version, charge_lines, price_at

T0 = datetime(2026, 11, 1, 0, 0, tzinfo=timezone.utc)
HOUR = timedelta(hours=1)
RANGE = {"start": "2026-11-01T00:00:00Z", "end": "2026-11-02T00:00:00Z"}


@pytest.fixture
def client():
    return TestClient(app, raise_server_exceptions=False)


def entitle(conn, tenant, status="active", grace_until=None):
    conn.execute("INSERT INTO entitlements (tenant_id, status, grace_until) VALUES (%s, %s, %s)", (tenant, status, grace_until))
    conn.commit()


def rollup(conn, tenant, model, hour_start, tokens_in, tokens_out):
    conn.execute("INSERT INTO usage_rollups_hourly (tenant_id, model, hour_start, tokens_in, tokens_out, event_count)"
                 " VALUES (%s, %s, %s, %s, %s, 1)", (tenant, model, hour_start, tokens_in, tokens_out))
    conn.commit()


def charges(client, tenant, header=None, **params):
    return client.get(f"/v1/tenants/{tenant}/charges", params=params or RANGE, headers={"X-Tenant-Id": header or tenant})


# --- edge: the tenant header ---

@pytest.fixture
def probe():
    """A one-route app behind the real edge, so any string can be a tenant id without URL escaping."""
    small = FastAPI()

    @small.get("/probe")
    def probe_route(tenant: str = Depends(require_entitlement)) -> dict[str, str]:
        return {"tenant": tenant}

    return TestClient(small, raise_server_exceptions=False)


@pytest.mark.parametrize("name", ["x-tenant-id", "X-TENANT-ID", "X-Tenant-Id", "x-TeNaNt-iD"])
def test_the_header_name_is_case_insensitive(probe, conn, name):
    entitle(conn, "tenant-1")
    assert probe.get("/probe", headers={name: "tenant-1"}).json() == {"tenant": "tenant-1"}


@pytest.mark.parametrize("value", ["", " ", "\t", "  \t "])
def test_an_empty_or_blank_tenant_header_is_401(probe, conn, value):
    entitle(conn, "tenant-1")
    assert probe.get("/probe", headers={"X-Tenant-Id": value}).status_code == 401


def test_spaces_around_the_tenant_header_are_trimmed_and_the_tenant_is_the_trimmed_one(probe, conn):
    entitle(conn, "tenant-1")
    assert probe.get("/probe", headers={"X-Tenant-Id": "  tenant-1  "}).json() == {"tenant": "tenant-1"}


def test_a_repeated_tenant_header_is_never_a_500(probe, conn):
    entitle(conn, "tenant-1")
    entitle(conn, "tenant-2")
    resp = probe.get("/probe", headers=[("X-Tenant-Id", "tenant-1"), ("X-Tenant-Id", "tenant-2")])
    assert resp.status_code == 200 and resp.json()["tenant"] in ("tenant-1", "tenant-2")


@pytest.mark.parametrize("tenant", ["x' OR '1'='1", "tenant-1'; DROP TABLE entitlements; --", "%", "_", "tenant-%", "ten_nt-1", "T" * 20_000, "../tenant-1", "tenant-1\\", "tenant-1,tenant-2"])
def test_odd_tenant_ids_are_matched_exactly_not_as_patterns(probe, conn, tenant):
    entitle(conn, "tenant-1")                                        # the only entitled tenant
    assert probe.get("/probe", headers={"X-Tenant-Id": tenant}).status_code == 402
    assert conn.execute("SELECT count(*) AS n FROM entitlements").fetchone()["n"] == 1


@pytest.mark.parametrize("variant", ["Tenant-1", "TENANT-1", "tenant-1 ", "tenant‐1", "tenant-1​"])
def test_look_alike_tenant_ids_are_other_tenants(probe, conn, variant):
    entitle(conn, "tenant-1")
    resp = probe.get("/probe", headers={"X-Tenant-Id": variant.encode("utf-8")})
    assert resp.status_code in (402, 401), resp.text                   # never the entitled tenant's access


def test_a_non_ascii_tenant_id_sent_as_utf8_matches_its_own_entitlement(probe, conn):
    # Stripe metadata is UTF-8; the server reads header bytes as latin-1. Without the re-decode "tenant-é" arrives as
    # "tenant-Ã©" and a tenant with a non-ASCII id could never be admitted.
    entitle(conn, "tenant-é")
    resp = probe.get("/probe", headers={"X-Tenant-Id": "tenant-é".encode("utf-8")})
    assert (resp.status_code, resp.json()) == (200, {"tenant": "tenant-é"})
    assert probe.get("/probe", headers={"X-Tenant-Id": b"tenant-\xe9"}).status_code in (200, 402)   # latin-1 bytes: no crash


def test_header_bytes_that_are_not_utf8_are_a_402_not_a_crash(probe, conn):
    entitle(conn, "tenant-1")
    assert probe.get("/probe", headers={"X-Tenant-Id": b"tenant-\xff\xfe"}).status_code == 402


# --- cross-tenant: the header, the path and the data must all name one tenant ---

def test_one_entitled_tenant_cannot_read_another_entitled_tenants_charges(client, conn):
    add_price_version(conn, "m1", T0, 1000, 2000)
    entitle(conn, "tenant-a")
    entitle(conn, "tenant-b")
    rollup(conn, "tenant-b", "m1", T0, 1000, 500)
    resp = charges(client, "tenant-b", header="tenant-a")
    assert resp.status_code == 403
    assert resp.json() == {"detail": "tenant does not match the caller"}
    assert charges(client, "tenant-a").json()["lines"] == []                # a's own view holds none of b's rows


@pytest.mark.parametrize("path_tenant", ["Tenant-A", "tenant-a%20", "%20tenant-a", "tenant-a%00", "tenant-a%2e", "tenant-b", "TENANT-A"])
def test_a_path_that_differs_from_the_header_in_any_way_is_403_or_404(client, conn, path_tenant):
    entitle(conn, "tenant-a")
    resp = client.get(f"/v1/tenants/{path_tenant}/charges", params=RANGE, headers={"X-Tenant-Id": "tenant-a"})
    assert resp.status_code in (403, 404), resp.text


def test_the_charges_route_is_402_for_a_tenant_without_an_entitlement_even_with_its_own_path(client, conn):
    rollup(conn, "tenant-z", "m1", T0, 1, 1)
    assert charges(client, "tenant-z").status_code == 402


# --- the price table ---

@pytest.mark.parametrize("bad", [1.5, 1.0, True, "1500", None, float("nan")])
def test_a_price_that_is_not_an_integer_is_refused_not_rounded(conn, bad):
    # PostgreSQL would round 1.5 to 2 and accept True as 1: money must never be silently rounded.
    with pytest.raises((TypeError, ValueError)):
        add_price_version(conn, "m1", T0, bad, 1000)
    with pytest.raises((TypeError, ValueError)):
        add_price_version(conn, "m1", T0, 1000, bad)
    assert conn.execute("SELECT count(*) AS n FROM price_versions").fetchone()["n"] == 0


def test_a_price_beyond_bigint_or_below_zero_is_refused_loudly(conn):
    with pytest.raises(psycopg.Error):
        add_price_version(conn, "m1", T0, 2**63, 1)
    conn.rollback()
    with pytest.raises(psycopg.Error):
        add_price_version(conn, "m1", T0, -1, 1)
    conn.rollback()
    assert conn.execute("SELECT count(*) AS n FROM price_versions").fetchone()["n"] == 0


def test_a_price_of_zero_is_a_price_and_no_price_is_an_error(conn):
    add_price_version(conn, "free", T0, 0, 0)
    assert price_at(conn, "free", T0)["input_micros_per_1k"] == 0
    with pytest.raises(NoPrice):
        price_at(conn, "free", T0 - timedelta(microseconds=1))
    with pytest.raises(NoPrice):
        price_at(conn, "other", T0)


def test_a_price_from_the_far_future_is_not_in_force_and_one_from_year_one_always_is(conn):
    add_price_version(conn, "m1", datetime(1, 1, 1, tzinfo=timezone.utc), 1, 1)
    add_price_version(conn, "m1", datetime(9999, 12, 31, 23, 59, 59, tzinfo=timezone.utc), 5, 5)
    assert price_at(conn, "m1", T0)["version"] == 1
    assert price_at(conn, "m1", datetime(9999, 12, 31, 23, 59, 59, tzinfo=timezone.utc))["version"] == 2


def test_the_price_in_force_is_found_across_utc_offsets(conn):
    add_price_version(conn, "m1", T0, 100, 100)
    plus14 = timezone(timedelta(hours=14))
    assert price_at(conn, "m1", datetime(2026, 11, 1, 14, 0, tzinfo=plus14))["version"] == 1       # == T0 exactly
    with pytest.raises(NoPrice):
        price_at(conn, "m1", datetime(2026, 11, 1, 13, 59, 59, 999999, tzinfo=plus14))             # one microsecond before T0


# --- rating at the extremes ---

BIGINT_MAX = 2**63 - 1


def test_the_largest_stored_tokens_times_the_largest_price_is_exact_through_the_route(client, conn):
    add_price_version(conn, "m1", T0, BIGINT_MAX, BIGINT_MAX)
    entitle(conn, "tenant-1")
    rollup(conn, "tenant-1", "m1", T0, BIGINT_MAX, BIGINT_MAX)
    expected = (BIGINT_MAX * BIGINT_MAX * 2 + 500) // 1000
    resp = charges(client, "tenant-1")
    assert resp.status_code == 200, resp.text
    assert resp.json()["total_micros"] == expected                       # an exact Python int, not a float
    assert resp.json()["lines"][0]["micros"] == expected
    assert isinstance(resp.json()["total_micros"], int)


def test_zero_tokens_cost_zero_and_still_show_as_a_line(client, conn):
    add_price_version(conn, "m1", T0, 1000, 2000)
    entitle(conn, "tenant-1")
    rollup(conn, "tenant-1", "m1", T0, 0, 0)
    body = charges(client, "tenant-1").json()
    assert body["total_micros"] == 0 and [line["micros"] for line in body["lines"]] == [0]


def test_charges_pick_the_same_version_as_price_at_at_every_boundary(conn):
    # charge_lines picks from versions it loaded once; price_at asks the database. They must never disagree.
    add_price_version(conn, "m1", T0, 1, 1)                              # v1
    add_price_version(conn, "m1", T0 + timedelta(minutes=90), 2, 2)      # v2: mid-hour
    add_price_version(conn, "m1", T0 + 3 * HOUR, 3, 3)                   # v3 and v4 share one instant: the later wins
    add_price_version(conn, "m1", T0 + 3 * HOUR, 4, 4)
    for h in range(8):
        rollup(conn, "tenant-1", "m1", T0 + h * HOUR, 1000, 0)
    lines, _ = charge_lines(conn, "tenant-1", T0, T0 + 8 * HOUR)
    assert [l["price_version"] for l in lines] == [price_at(conn, "m1", T0 + h * HOUR)["version"] for h in range(8)]
    assert [l["price_version"] for l in lines] == [1, 1, 2, 4, 4, 4, 4, 4]


def test_a_sum_of_lines_is_exact_even_when_each_line_rounds(conn):
    add_price_version(conn, "m1", T0, 1500, 0)                           # 1 token = 1.5 micros, which rounds to 2
    for h in range(10):
        rollup(conn, "tenant-1", "m1", T0 + h * HOUR, 1, 0)
    lines, total = charge_lines(conn, "tenant-1", T0, T0 + 10 * HOUR)
    assert [l["micros"] for l in lines] == [2] * 10 and total == 20      # rounded per line, never 15 on the whole


# --- charges ranges ---

def test_a_gap_before_the_first_price_fails_the_whole_request_409_and_names_the_model(client, conn):
    add_price_version(conn, "m1", T0 + HOUR, 1000, 1000)                 # in force from hour 1 only
    entitle(conn, "tenant-1")
    rollup(conn, "tenant-1", "m1", T0, 10, 10)                           # hour 0: no price yet
    rollup(conn, "tenant-1", "m1", T0 + HOUR, 10, 10)
    resp = charges(client, "tenant-1")
    assert resp.status_code == 409 and "m1" in resp.json()["detail"]
    assert "lines" not in resp.json()                                    # no partial bill


def test_one_unpriced_model_among_priced_ones_is_409_not_a_bill_without_it(client, conn):
    add_price_version(conn, "m1", T0, 1000, 1000)
    entitle(conn, "tenant-1")
    rollup(conn, "tenant-1", "m1", T0, 10, 10)
    rollup(conn, "tenant-1", "m2", T0, 10, 10)
    assert charges(client, "tenant-1").status_code == 409


@pytest.mark.parametrize("params", [
    {"start": "2026-11-01T00:00:00Z", "end": "2026-11-01T00:00:00Z"},                 # start == end
    {"start": "2026-11-01T00:00:00", "end": "2026-11-02T00:00:00Z"},                  # naive start
    {"start": "2026-11-01T00:00:00Z", "end": "2026-11-02T00:00:00"},                  # naive end
    {"start": "2026-11-01", "end": "2026-11-02"},                                     # dates only
    {"start": "", "end": "2026-11-02T00:00:00Z"}, {"end": "2026-11-02T00:00:00Z"}, {"start": "2026-11-01T00:00:00Z"}, {},
    {"start": "yesterday", "end": "now"}, {"start": "2026-13-45T00:00:00Z", "end": "2026-11-02T00:00:00Z"},
    {"start": "9" * 5000, "end": "2026-11-02T00:00:00Z"}, {"start": "NaN", "end": "Infinity"},
    {"start": "2026-11-02T00:00:00Z", "end": "2026-11-01T00:00:00Z"},                 # backwards
])
def test_bad_ranges_are_422_never_a_500(client, conn, params):
    entitle(conn, "tenant-1")
    resp = client.get("/v1/tenants/tenant-1/charges", params=params, headers={"X-Tenant-Id": "tenant-1"})
    assert resp.status_code == 422, resp.text


@pytest.mark.parametrize("start, end", [
    ("0001-01-01T00:00:00Z", "9999-12-31T23:59:59Z"),                                 # the whole calendar
    ("0001-01-01T00:00:00+05:00", "0001-01-02T00:00:00+05:00"),                       # before UTC year one
    ("9999-12-31T00:00:00-12:00", "9999-12-31T23:59:59.999999-12:00"),                # past UTC year 9999
    ("2000-01-01T00:00:00Z", "2100-01-01T00:00:00Z"),
    ("1969-12-31T23:59:59Z", "1970-01-01T00:00:01Z"),                                 # across the epoch
])
def test_extreme_ranges_are_answered_not_crashed(client, conn, start, end):
    entitle(conn, "tenant-1")
    resp = client.get("/v1/tenants/tenant-1/charges", params={"start": start, "end": end}, headers={"X-Tenant-Id": "tenant-1"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["lines"] == [] and resp.json()["total_micros"] == 0


def test_the_same_instants_in_other_offsets_give_the_same_bill(client, conn):
    add_price_version(conn, "m1", T0, 1000, 2000)
    entitle(conn, "tenant-1")
    for h in range(3):
        rollup(conn, "tenant-1", "m1", T0 + h * HOUR, 1000, 500)
    in_utc = charges(client, "tenant-1", start="2026-11-01T00:00:00Z", end="2026-11-01T02:00:00Z").json()
    # the same two instants: 2026-11-01T00:00Z written at +14:00, and 2026-11-01T02:00Z written at -12:00
    shifted = charges(client, "tenant-1", start="2026-11-01T14:00:00+14:00", end="2026-10-31T14:00:00-12:00").json()
    assert shifted["total_micros"] == in_utc["total_micros"] == 2 * 2000
    assert len(shifted["lines"]) == len(in_utc["lines"]) == 2


def test_a_three_year_range_of_hourly_rows_is_billed_in_reasonable_time(conn):
    add_price_version(conn, "m1", T0, 1000, 2000)
    add_price_version(conn, "m2", T0, 3, 4)
    conn.execute(
        "INSERT INTO usage_rollups_hourly (tenant_id, model, hour_start, tokens_in, tokens_out, event_count)"
        " SELECT 'tenant-1', m, %s + g * interval '1 hour', 10, 10, 1 FROM generate_series(0, 4999) g, unnest(ARRAY['m1','m2']) m", (T0,))
    conn.commit()
    started = time.monotonic()
    lines, total = charge_lines(conn, "tenant-1", T0, T0 + timedelta(days=3 * 365))
    elapsed = time.monotonic() - started
    assert len(lines) == 10_000 and total == 5000 * 30                  # m1 30 micros an hour; m2 0.07 rounds to 0
    assert elapsed < 3, f"10,000 rows took {elapsed:.1f}s: one price lookup per row"


# --- the whole route with the real edge: header, path and body must name one tenant ---

def usage_body(tenant, key="k1"):
    return {"tenant_id": tenant, "idempotency_key": key, "model": "m1", "tokens_in": 1, "tokens_out": 1, "occurred_at": "2026-11-01T00:30:00Z"}


def test_usage_for_another_tenant_is_403_and_stored_nowhere_even_when_both_are_entitled(client, conn):
    entitle(conn, "tenant-a")
    entitle(conn, "tenant-b")
    assert client.post("/v1/usage", json=usage_body("tenant-b"), headers={"X-Tenant-Id": "tenant-a"}).status_code == 403
    assert client.post("/v1/usage", json=usage_body("tenant-a", "k2"), headers={"X-Tenant-Id": "tenant-b"}).status_code == 403
    assert conn.execute("SELECT count(*) AS n FROM usage_events").fetchone()["n"] == 0
    assert client.post("/v1/usage", json=usage_body("tenant-a"), headers={"X-Tenant-Id": "tenant-a"}).status_code == 201


def test_a_canceled_tenant_cannot_post_or_read_with_its_own_name(client, conn):
    entitle(conn, "tenant-c", status="canceled")
    assert client.post("/v1/usage", json=usage_body("tenant-c"), headers={"X-Tenant-Id": "tenant-c"}).status_code == 402
    assert charges(client, "tenant-c").status_code == 402
    assert conn.execute("SELECT count(*) AS n FROM usage_events").fetchone()["n"] == 0


def test_a_non_ascii_tenant_can_use_the_whole_route(client, conn):
    # Header as UTF-8 bytes, path percent-encoded, body as JSON: three spellings of one tenant must agree.
    add_price_version(conn, "m1", T0, 1000, 2000)
    entitle(conn, "tenant-é")
    rollup(conn, "tenant-é", "m1", T0, 1000, 500)
    resp = client.get("/v1/tenants/tenant-%C3%A9/charges", params=RANGE, headers={"X-Tenant-Id": "tenant-é".encode("utf-8")})
    assert resp.status_code == 200 and resp.json()["total_micros"] == 2000
    posted = client.post("/v1/usage", json=usage_body("tenant-é"), headers={"X-Tenant-Id": "tenant-é".encode("utf-8")})
    assert posted.status_code == 201


# --- the 422 body itself must be serializable: FastAPI echoes the offending input ---

@pytest.mark.parametrize("query", [
    "start=NaN&end=Infinity", "start=-Infinity&end=1e999", "start=%ED%A0%80&end=%ED%B0%80",          # lone surrogates, UTF-8 encoded
    "start=%FF%FE&end=%C0%AF", "start=%00&end=%00", "start=2026-11-01T00:00:00Z%00&end=2026-11-02T00:00:00Z",
    "start=%F0%9F%98%80&end=%E3%83%86", "start=" + "%E3" * 3000 + "&end=x", "start[]=1&end[]=2",
    "start=2026-11-01T00:00:00Z&start=2026-11-01T00:00:00Z&end=junk",
])
def test_hostile_query_strings_are_422_never_a_500(client, conn, query):
    entitle(conn, "tenant-1")
    resp = client.get(f"/v1/tenants/tenant-1/charges?{query}", headers={"X-Tenant-Id": "tenant-1"})
    assert resp.status_code == 422, resp.text
    assert isinstance(resp.json()["detail"], (str, list))


@pytest.mark.parametrize("path", ["/v1/tenants/%ED%A0%80/charges", "/v1/tenants/%FF/charges", "/v1/tenants/%00/charges", "/v1/tenants/%E3%83%86/charges"])
def test_hostile_path_tenants_are_403_never_a_500(client, conn, path):
    entitle(conn, "tenant-1")
    resp = client.get(path + "?" + "&".join(f"{k}={v}" for k, v in RANGE.items()), headers={"X-Tenant-Id": "tenant-1"})
    assert resp.status_code == 403, resp.text


# --- start and end are ISO 8601 text, never Unix seconds ---

@pytest.mark.parametrize("value", ["5", "0", "-5", "1762770600", "1762770600.5", "1e9", "20261101", "+5", " 5", "٥"])
@pytest.mark.parametrize("which", ["start", "end"])
def test_a_bare_number_is_not_a_time(client, conn, which, value):
    entitle(conn, "tenant-1")
    params = {"start": "2026-11-01T00:00:00Z", "end": "2026-11-02T00:00:00Z", which: value}
    resp = client.get("/v1/tenants/tenant-1/charges", params=params, headers={"X-Tenant-Id": "tenant-1"})
    assert resp.status_code == 422, resp.text
    assert "ISO 8601" in resp.text


def test_every_iso_spelling_still_gives_the_same_bill(client, conn):
    add_price_version(conn, "m1", T0, 1000, 2000)
    entitle(conn, "tenant-1")
    rollup(conn, "tenant-1", "m1", T0, 1000, 500)
    spellings = [("2026-11-01T00:00:00Z", "2026-11-01T01:00:00Z"), ("2026-11-01T00:00:00+00:00", "2026-11-01T01:00:00+00:00"),
                 ("2026-11-01T00:00:00.000000Z", "2026-11-01T01:00:00.0Z"), ("2026-11-01T02:00:00+02:00", "2026-11-01T03:00:00+02:00"),
                 ("2026-10-31T19:00:00-05:00", "2026-10-31T20:00:00-05:00"), ("2026-11-01 00:00:00Z", "2026-11-01 01:00:00Z")]
    totals = {charges(client, "tenant-1", start=s, end=e).json()["total_micros"] for s, e in spellings}
    assert totals == {2000}
