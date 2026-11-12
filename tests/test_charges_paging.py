"""GET /v1/tenants/{id}/charges answers in pages (`limit`, `cursor`) and never as a silently short bill.

The oracle is a frozen copy of `charge_lines` as it was before paging: pages put together must equal its lines and its total, and
it must refuse (NoPrice) in the same cases. See docs/billing.md, "Paging".
"""

import base64
import json
import math
import time
import tracemalloc
from datetime import datetime, timedelta, timezone

import psycopg
import pytest
from fastapi.testclient import TestClient

from app import db, pricing
from app.edge import require_entitlement
from app.main import app
from tests.conftest import own_pool_sessions
from app.pricing import NoPrice, add_price_version

T0 = datetime(2026, 11, 1, 0, 0, tzinfo=timezone.utc)
HOUR = timedelta(hours=1)
PATH = "/v1/tenants/tenant-1/charges"


@pytest.fixture
def client():
    app.dependency_overrides[require_entitlement] = lambda: "tenant-1"
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()


def old_charge_lines(conn, tenant_id, start, end):
    """charge_lines before paging (main f3d8044), kept as the oracle. Do not "improve" it."""
    rows = conn.execute(
        "SELECT model, hour_start, tokens_in, tokens_out FROM usage_rollups_hourly"
        " WHERE tenant_id = %s AND hour_start >= %s AND hour_start < %s ORDER BY hour_start, model",
        (tenant_id, start, end),
    ).fetchall()
    models = sorted({row["model"] for row in rows})
    loaded = {model: [] for model in models}
    if models:
        for v in conn.execute(
            "SELECT model, version, effective_from, input_micros_per_1k, output_micros_per_1k FROM price_versions"
            " WHERE model = ANY(%s) AND effective_from <= %s ORDER BY effective_from, version",
            (models, rows[-1]["hour_start"]),
        ):
            loaded[v["model"]].append(v)
    keys = {
        model: [v["effective_from"].astimezone(timezone.utc) for v in versions] for model, versions in loaded.items()
    }
    lines = []
    unpriced = {}
    for row in rows:
        version = pricing._in_force(keys[row["model"]], loaded[row["model"]], row["hour_start"])
        if version is None:
            unpriced.setdefault(row["model"], row["hour_start"])
            continue
        lines.append(
            {
                "model": row["model"],
                "hour_start": row["hour_start"],
                "tokens_in": row["tokens_in"],
                "tokens_out": row["tokens_out"],
                "price_version": version["version"],
                "micros": pricing._rate(version, row["tokens_in"], row["tokens_out"]),
            }
        )
    if unpriced:
        raise NoPrice(pricing._unpriced_message(unpriced))
    return lines, sum(line["micros"] for line in lines)


def rollup(conn, model, hour, tokens_in=1000, tokens_out=500, tenant="tenant-1"):
    conn.execute(
        "INSERT INTO usage_rollups_hourly (tenant_id, model, hour_start, tokens_in, tokens_out, event_count)"
        " VALUES (%s, %s, %s, %s, %s, 1)",
        (tenant, model, hour, tokens_in, tokens_out),
    )
    conn.commit()


def span(hours, *, start=T0):
    return {"start": start.isoformat(), "end": (start + hours * HOUR).isoformat()}


def as_oracle(lines):
    """Response lines with hour_start parsed, so they compare with the oracle's datetimes."""
    return [{**line, "hour_start": datetime.fromisoformat(line["hour_start"])} for line in lines]


def walk(client, params, limit, path=PATH):
    """Every page of the range: (all lines, the pages' bodies). Each page must be a full 200 and agree on the whole-range numbers."""
    lines, pages, cursor = [], [], None
    while True:
        query = {**params, "limit": limit, **({"cursor": cursor} if cursor else {})}
        resp = client.get(path, params=query)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        pages.append(body)
        lines += body["lines"]
        cursor = body["next_cursor"]
        if cursor is None:
            return lines, pages
        assert len(pages) < 10_000, "paging does not end"


MODELS = ["Zebra", "apple", "Apple", "zebra", "mango", "Äpfel", "éclair", "日本語", "m-1", "m_1"]


@pytest.fixture
def rich(conn):
    """12 hours x 10 models (mixed case, non-ASCII), a price change at hour 5, a second tenant, rows exactly at start and end."""
    for i, model in enumerate(MODELS):
        add_price_version(conn, model, T0 - timedelta(days=1), 1000 + i, 2000 + 3 * i)
        add_price_version(conn, model, T0 + 5 * HOUR, 1500 + i, 100 + i)
    for h in range(12):
        for i, model in enumerate(MODELS):
            if (h + i) % 3:  # not every model in every hour
                rollup(conn, model, T0 + h * HOUR, 100 * h + i + 1, 37 * i + h)
    rollup(conn, "Zebra", T0 + 2 * HOUR, 5, 5, tenant="tenant-2")
    rollup(conn, "Zebra", T0 + 12 * HOUR, 999, 999)  # exactly at `end`: not in the range
    rollup(conn, "Zebra", T0 - HOUR, 999, 999)  # just before `start`
    return span(12)


@pytest.mark.parametrize("limit", [1, 2, 3, 7, 10, 11, 40, 1000])
def test_pages_put_together_equal_the_unpaged_charge_lines(client, conn, rich, limit):
    start, end = T0, T0 + 12 * HOUR
    want_lines, want_total = old_charge_lines(conn, "tenant-1", start, end)
    assert len(want_lines) > 40
    lines, pages = walk(client, rich, limit)
    assert as_oracle(lines) == want_lines
    assert len(pages) == math.ceil(len(want_lines) / limit)
    for page in pages:
        assert page["total_micros"] == want_total  # the whole range on every page, not the page's share
        assert page["line_count"] == len(want_lines)
        assert len(page["lines"]) <= limit
    assert sum(line["micros"] for line in lines) == want_total
    assert all(page["next_cursor"] for page in pages[:-1]) and pages[-1]["next_cursor"] is None


def test_without_limit_the_body_is_the_whole_range_with_a_count_and_no_cursor(client, conn, rich):
    want_lines, want_total = old_charge_lines(conn, "tenant-1", T0, T0 + 12 * HOUR)
    body = client.get(PATH, params=rich).json()
    assert as_oracle(body["lines"]) == want_lines
    assert (body["total_micros"], body["line_count"], body["next_cursor"]) == (want_total, len(want_lines), None)
    assert body["unclosed_hours_truncated"] is False


def test_mixed_case_models_page_in_the_database_order_across_a_page_boundary(client, conn):
    for model in ("Zebra", "apple", "Banana", "cherry"):
        add_price_version(conn, model, T0 - HOUR, 1000, 1000)
        rollup(conn, model, T0)
        rollup(conn, model, T0 + HOUR)
    in_database_order = [
        r["model"]
        for r in conn.execute("SELECT model FROM usage_rollups_hourly WHERE hour_start = %s ORDER BY model", (T0,))
    ]
    if in_database_order == sorted(in_database_order):
        pytest.skip(
            "this database orders model like Python's sorted(), so a Python-side page boundary could not differ"
        )
    lines, _ = walk(client, span(2), 1)
    assert [(l["hour_start"], l["model"]) for l in lines] == [
        (h.isoformat().replace("+00:00", "Z"), m) for h in (T0, T0 + HOUR) for m in in_database_order
    ]


def test_an_empty_range_is_one_empty_page(client, conn):
    for params in ({}, {"limit": 5}):
        body = client.get(PATH, params={**span(3), **params}).json()
        assert (body["lines"], body["total_micros"], body["line_count"], body["next_cursor"]) == ([], 0, 0, None)


def test_one_line_is_one_page(client, conn):
    add_price_version(conn, "m1", T0, 1000, 2000)
    rollup(conn, "m1", T0)
    lines, pages = walk(client, span(3), 1)
    assert len(lines) == 1 and len(pages) == 1 and pages[0]["next_cursor"] is None


def test_a_range_with_rows_only_at_its_edges(client, conn):
    add_price_version(conn, "m1", T0 - HOUR, 1000, 1000)
    rollup(conn, "m1", T0)  # start is included
    rollup(conn, "m1", T0 + 3 * HOUR)  # end is excluded
    lines, pages = walk(client, span(3), 1)
    assert [l["hour_start"] for l in lines] == ["2026-11-01T00:00:00Z"] and len(pages) == 1


def test_prices_dated_inside_the_range_apply_and_one_dated_at_or_after_the_end_does_not(client, conn):
    add_price_version(conn, "m1", T0 - HOUR, 1000, 1000)
    add_price_version(conn, "m1", T0 + 2 * HOUR, 3000, 3000)  # inside the range: applies from hour 2
    add_price_version(conn, "m1", T0 + 4 * HOUR, 9000, 9000)  # exactly at `end`: can apply to no hour of the range
    add_price_version(conn, "m1", T0 + 9 * HOUR, 9000, 9000)  # after `end`
    for h in range(4):
        rollup(conn, "m1", T0 + h * HOUR)
    want, total = old_charge_lines(conn, "tenant-1", T0, T0 + 4 * HOUR)
    assert [l["price_version"] for l in want] == [1, 1, 2, 2]
    lines, pages = walk(client, span(4), 3)
    assert as_oracle(lines) == want and pages[0]["total_micros"] == total


def test_an_unpriced_model_on_a_later_page_is_a_409_on_the_first_page(client, conn):
    add_price_version(conn, "m1", T0 - HOUR, 1000, 1000)
    for h in range(4):
        rollup(conn, "m1", T0 + h * HOUR)
    rollup(conn, "zz-unpriced", T0 + 3 * HOUR)
    with pytest.raises(NoPrice) as oracle:
        old_charge_lines(conn, "tenant-1", T0, T0 + 4 * HOUR)
    resp = client.get(PATH, params={**span(4), "limit": 1})
    assert (resp.status_code, resp.json()) == (409, {"detail": str(oracle.value)})
    assert client.get(PATH, params=span(4)).status_code == 409


def test_a_price_missing_after_page_one_is_a_409_on_page_two_and_never_a_short_bill(client, conn):
    add_price_version(conn, "m1", T0 - HOUR, 1000, 1000)
    for h in range(4):
        rollup(conn, "m1", T0 + h * HOUR)
    first = client.get(PATH, params={**span(4), "limit": 2}).json()
    rollup(conn, "zz-new", T0 + 3 * HOUR)
    resp = client.get(PATH, params={**span(4), "limit": 2, "cursor": first["next_cursor"]})
    assert resp.status_code == 409 and "zz-new" in resp.json()["detail"]


# --- the contract ---------------------------------------------------------------------------------------------------------


def test_without_limit_a_range_over_the_cap_is_422_and_never_a_short_200(client, conn, monkeypatch):
    add_price_version(conn, "m1", T0 - HOUR, 1000, 1000)
    for h in range(6):
        rollup(conn, "m1", T0 + h * HOUR)
    monkeypatch.setattr(pricing, "LINE_CAP", 5)
    resp = client.get(PATH, params=span(6))
    assert resp.status_code == 422 and "page with limit" in resp.json()["detail"]
    assert client.get(PATH, params=span(5)).json()["line_count"] == 5  # exactly the cap is still a whole body
    assert client.get(PATH, params={**span(6), "limit": 5}).status_code == 200


@pytest.mark.parametrize("limit", ["0", "-1", "abc", "1.5", "", str(pricing.MAX_PAGE + 1), "1e3"])
def test_a_limit_outside_1_to_max_page_is_422(client, conn, limit):
    assert client.get(PATH, params={**span(3), "limit": limit}).status_code == 422


def test_the_largest_limit_is_accepted(client, conn):
    assert client.get(PATH, params={**span(3), "limit": pricing.MAX_PAGE}).status_code == 200


def test_a_cursor_without_a_limit_is_422(client, conn):
    add_price_version(conn, "m1", T0 - HOUR, 1000, 1000)
    for h in range(3):
        rollup(conn, "m1", T0 + h * HOUR)
    cursor = client.get(PATH, params={**span(3), "limit": 1}).json()["next_cursor"]
    assert client.get(PATH, params={**span(3), "cursor": cursor}).status_code == 422


def make_cursor(**fields):
    """A cursor as JSON in base64; json.dumps keeps a lone surrogate as an escape, so the text stays ASCII."""
    body = {
        "t": "tenant-1",
        "s": T0.isoformat(),
        "e": (T0 + 3 * HOUR).isoformat(),
        "h": T0.isoformat(),
        "m": "m1",
        "n": 1,
        "u": 2000,
    }
    body.update(fields)
    return base64.b64encode(json.dumps(body).encode()).decode()


@pytest.fixture
def three_hours(conn):
    add_price_version(conn, "m1", T0 - HOUR, 1000, 1000)
    for h in range(3):
        rollup(conn, "m1", T0 + h * HOUR)


def page_one(client):
    return client.get(PATH, params={**span(3), "limit": 1}).json()


def test_the_cursor_a_page_hands_out_is_accepted_back(client, conn, three_hours):
    first = page_one(client)
    resp = client.get(PATH, params={**span(3), "limit": 1, "cursor": first["next_cursor"]})
    assert resp.status_code == 200 and resp.json()["lines"][0]["hour_start"] == "2026-11-01T01:00:00Z"


def test_the_same_range_spelled_in_another_offset_is_the_same_range(client, conn, three_hours):
    first = page_one(client)
    other = {"start": "2026-11-01T02:00:00+02:00", "end": "2026-11-01T03:00:00Z"}
    resp = client.get(PATH, params={**other, "limit": 1, "cursor": first["next_cursor"]})
    assert resp.status_code == 200


def _flip(cursor):
    return cursor[:-6] + ("A" if cursor[-6] != "A" else "B") + cursor[-5:]


@pytest.mark.parametrize(
    "token",
    [
        pytest.param("", id="empty"),
        pytest.param("not base64!!", id="not base64"),
        pytest.param("日本語", id="non-ascii"),
        pytest.param(base64.b64encode(b"not json").decode(), id="not json"),
        pytest.param(base64.b64encode(b"[1, 2]").decode(), id="not an object"),
        pytest.param(base64.b64encode(b"{}").decode(), id="no fields"),
        pytest.param(make_cursor(n=True), id="bool count"),
        pytest.param(make_cursor(n=-1), id="negative count"),
        pytest.param(make_cursor(n="1"), id="text count"),
        pytest.param(make_cursor(u=1.5), id="float micros"),
        pytest.param(make_cursor(m=5), id="model not text"),
        pytest.param(make_cursor(h="yesterday"), id="hour not a date"),
        pytest.param(make_cursor(h="2026-11-01T00:00:00"), id="hour without a zone"),
        pytest.param(make_cursor(x=1), id="extra field"),
        pytest.param(make_cursor(m="a\x00b"), id="NUL in the model"),
        pytest.param(make_cursor(m="a\ud800b"), id="lone surrogate in the model"),
        pytest.param(make_cursor(m=""), id="empty model"),
        pytest.param(make_cursor(m="x" * 201), id="model over the ingest limit"),
        pytest.param(make_cursor(h=(T0 + 3 * HOUR).isoformat()), id="hour at the end of the range"),
        pytest.param(make_cursor(h=(T0 - HOUR).isoformat()), id="hour before the range"),
        pytest.param(make_cursor(h="0001-01-01T00:00:00+14:00"), id="hour in year one"),
        pytest.param("A" * 5000, id="too long"),
    ],
)
def test_a_malformed_cursor_is_422(client, conn, three_hours, token):
    resp = client.get(PATH, params={**span(3), "limit": 1, "cursor": token})
    assert resp.status_code == 422


def test_a_truncated_or_tampered_cursor_is_422_or_409_never_a_page(client, conn, three_hours):
    cursor = page_one(client)["next_cursor"]
    for bad in (cursor[:-4], cursor[:10], _flip(cursor)):
        assert client.get(PATH, params={**span(3), "limit": 1, "cursor": bad}).status_code in (409, 422)
    assert client.get(PATH, params={**span(3), "limit": 1, "cursor": cursor[:-4]}).status_code == 422


def test_a_cursor_from_another_range_or_tenant_is_422_foreign(client, conn, three_hours):
    cursor = page_one(client)["next_cursor"]
    for params in ({**span(2)}, {**span(3, start=T0 + HOUR)}):
        resp = client.get(PATH, params={**params, "limit": 1, "cursor": cursor})
        assert resp.status_code == 422 and "foreign cursor" in resp.json()["detail"]
    app.dependency_overrides[require_entitlement] = lambda: "tenant-2"
    resp = client.get("/v1/tenants/tenant-2/charges", params={**span(3), "limit": 1, "cursor": cursor})
    assert resp.status_code == 422 and "foreign cursor" in resp.json()["detail"]


def test_a_cursor_whose_counts_were_edited_is_409_range_changed(client, conn, three_hours):
    for edit in ({"n": 2}, {"u": 1}):
        resp = client.get(PATH, params={**span(3), "limit": 1, "cursor": make_cursor(**edit)})
        assert resp.status_code == 409 and "range changed, restart" in resp.json()["detail"]


def test_an_hour_closed_out_of_order_behind_the_cursor_is_409_range_changed(client, conn):
    add_price_version(conn, "m1", T0 - HOUR, 1000, 1000)
    for h in (2, 3, 4):
        rollup(conn, "m1", T0 + h * HOUR)
    first = client.get(PATH, params={**span(6), "limit": 2}).json()
    rollup(conn, "m1", T0 + HOUR)  # an earlier hour closes late, behind the cursor
    resp = client.get(PATH, params={**span(6), "limit": 2, "cursor": first["next_cursor"]})
    assert resp.status_code == 409 and "range changed, restart" in resp.json()["detail"]


def test_a_price_backdated_behind_the_cursor_is_409_range_changed(client, conn, three_hours):
    first = page_one(client)
    add_price_version(conn, "m1", T0, 5000, 5000)  # applies from hour 0 on: line 1 is no longer what the client holds
    resp = client.get(PATH, params={**span(3), "limit": 1, "cursor": first["next_cursor"]})
    assert resp.status_code == 409 and "range changed, restart" in resp.json()["detail"]


def test_an_hour_closed_after_the_cursor_is_a_page_the_client_rule_still_checks(client, conn):
    add_price_version(conn, "m1", T0 - HOUR, 1000, 1000)
    for h in (0, 1):
        rollup(conn, "m1", T0 + h * HOUR)
    first = client.get(PATH, params={**span(6), "limit": 1}).json()
    rollup(conn, "m1", T0 + 3 * HOUR)  # ahead of the cursor: nothing the client holds changes
    lines, last = list(first["lines"]), first
    while last["next_cursor"]:
        last = client.get(PATH, params={**span(6), "limit": 1, "cursor": last["next_cursor"]}).json()
        lines += last["lines"]
    assert len(lines) == last["line_count"] == 3
    assert (
        sum(l["micros"] for l in lines) == last["total_micros"]
    )  # the client rule holds; page one's total (2000) was stale


def test_an_unpaged_and_a_paged_walk_of_a_big_page_agree(client, conn):
    add_price_version(conn, "m1", T0 - HOUR, 1000, 1000)
    for h in range(300):
        rollup(conn, "m1", T0 + h * HOUR, h, h)
    want, total = old_charge_lines(conn, "tenant-1", T0, T0 + 300 * HOUR)
    lines, pages = walk(client, span(300), 299)
    assert as_oracle(lines) == want and pages[0]["total_micros"] == total and len(pages) == 2


# --- unclosed hours: the cap says so ----------------------------------------------------------------------------------------


def events(conn, count):
    conn.execute(
        "INSERT INTO usage_events (tenant_id, idempotency_key, model, tokens_in, tokens_out, occurred_at)"
        " SELECT 'tenant-1', 'k' || i, 'm1', 1, 1, %s::timestamptz + i * interval '1 hour' FROM generate_series(0, %s) i",
        (T0, count - 1),
    )
    conn.commit()


@pytest.mark.parametrize("hours, truncated", [(pricing.UNCLOSED_LIMIT, False), (pricing.UNCLOSED_LIMIT + 1, True)])
def test_the_unclosed_hours_list_says_when_it_was_cut(client, conn, hours, truncated):
    events(conn, hours)
    body = client.get(PATH, params=span(hours + 5)).json()
    assert len(body["unclosed_hours"]) == pricing.UNCLOSED_LIMIT
    assert body["unclosed_hours_truncated"] is truncated


# --- the deadline ---------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("params", [{"limit": 5}, {}], ids=["paged", "unpaged"])
def test_a_pass_past_the_request_deadline_is_504_and_releases_its_connection(client, conn, monkeypatch, params):
    add_price_version(conn, "m1", T0 - HOUR, 1000, 1000)
    for h in range(20):
        rollup(conn, "m1", T0 + h * HOUR)
    monkeypatch.setattr(db, "REQUEST_STATEMENT_TIMEOUT_MS", 300)
    real = pricing._rate

    def slow(*args):
        time.sleep(0.05)
        return real(*args)

    monkeypatch.setattr(pricing, "_rate", slow)
    started = time.monotonic()
    resp = client.get(PATH, params={**span(20), **params})
    assert (resp.status_code, resp.json()) == (504, {"detail": "query timed out"})
    assert time.monotonic() - started < 3  # 20 rows x 50 ms would be 1 s: it stopped at 0.3 s
    stats = db._request_pool().get_stats()  # the connection went back to the pool (which keeps it open)
    assert stats["pool_size"] - stats["pool_available"] == 0
    assert (
        own_pool_sessions(conn, state_like="idle in transaction%") == 0
    )  # and went back with its transaction rolled back


# --- memory ---------------------------------------------------------------------------------------------------------------


def seed_big(conn, models, hours):
    conn.execute(
        "INSERT INTO price_versions (model, version, effective_from, input_micros_per_1k, output_micros_per_1k)"
        " SELECT 'model-' || m, 1, %s, 1000, 2000 FROM generate_series(1, %s) m",
        (T0 - HOUR, models),
    )
    conn.execute(
        "INSERT INTO usage_rollups_hourly (tenant_id, model, hour_start, tokens_in, tokens_out, event_count)"
        " SELECT 'tenant-1', 'model-' || m, %s::timestamptz + h * interval '1 hour', 1000 + h, 500 + m, 1"
        " FROM generate_series(1, %s) m, generate_series(0, %s) h",
        (T0, models, hours - 1),
    )
    conn.execute("ANALYZE usage_rollups_hourly")
    conn.commit()


def peak_of(call):
    tracemalloc.start()
    try:
        result = call()
        return result, tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


FIRST_PAGE_PEAK_BOUND = 20 * 2**20


def test_a_year_of_114_models_pages_in_bounded_memory_and_unpaged_is_refused_without_building_it(
    client, conn, monkeypatch, capsys
):
    monkeypatch.setattr(
        db, "REQUEST_STATEMENT_TIMEOUT_MS", 0
    )  # tracemalloc slows the pass; the deadline is tested above
    seed_big(conn, 114, 366 * 24)
    assert conn.execute("SELECT count(*) AS n FROM usage_rollups_hourly").fetchone()["n"] == 114 * 366 * 24
    params = {"start": T0.isoformat(), "end": (T0 + timedelta(days=366)).isoformat()}
    resp, paged = peak_of(lambda: client.get(PATH, params={**params, "limit": 100}))
    assert resp.status_code == 200
    body = resp.json()
    assert body["line_count"] == 114 * 366 * 24 and len(body["lines"]) == 100 and body["next_cursor"]
    resp, unpaged = peak_of(lambda: client.get(PATH, params=params))
    assert resp.status_code == 422 and "page with limit" in resp.json()["detail"]
    with capsys.disabled():
        print(
            f"\ntracemalloc peak, 1,002,096 rows: first page (limit 100) {paged / 2**20:.1f} MiB; unpaged refused {unpaged / 2**20:.1f} MiB"
        )
    assert paged < FIRST_PAGE_PEAK_BOUND and unpaged < FIRST_PAGE_PEAK_BOUND


def test_forty_largest_pages_at_once_fit_in_a_stated_budget(client, conn, capsys):
    hours = pricing.MAX_PAGE // 2
    seed_big(conn, 2, hours)  # exactly MAX_PAGE rows, inside the 366-day range
    params = {"start": T0.isoformat(), "end": (T0 + hours * HOUR).isoformat(), "limit": pricing.MAX_PAGE}
    resp, peak = peak_of(lambda: client.get(PATH, params=params))
    assert (
        resp.status_code == 200 and len(resp.json()["lines"]) == pricing.MAX_PAGE and resp.json()["next_cursor"] is None
    )
    with capsys.disabled():
        print(
            f"\nmax page: {pricing.MAX_PAGE} lines, body {len(resp.content) / 2**20:.2f} MiB, tracemalloc peak {peak / 2**20:.1f} MiB,"
            f" x40 = {40 * peak / 2**20:.0f} MiB"
        )
    assert 40 * peak < MAX_PAGE_BUDGET


MAX_PAGE_BUDGET = 512 * 2**20


# --- the pool hands out an idle connection: SET TRANSACTION must be the first statement ----------------------------------------

SNAPSHOT = "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"


def test_a_fresh_pooled_connection_accepts_the_snapshot_statement_first(conn, monkeypatch):
    db.close_pool()
    with db.request_connection() as c:  # just made by the pool, configured, never used
        assert c.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
        c.execute(SNAPSHOT)
        assert c.execute("SHOW transaction_isolation").fetchone()["transaction_isolation"] == "repeatable read"


def test_a_reused_pooled_connection_accepts_it_too_after_work_and_after_a_failure(conn, monkeypatch, client):
    monkeypatch.setenv("DB_POOL_MAX", "1")  # one connection: every borrow is the one just returned
    add_price_version(conn, "m1", T0 - HOUR, 1000, 1000)
    rollup(conn, "m1", T0)
    pids = set()
    with db.request_connection() as c:  # a borrower that used the connection, then took the lock path
        pids.add(c.info.backend_pid)
        c.execute(SNAPSHOT)
        c.execute("SELECT 1")
    with pytest.raises(RuntimeError):
        with db.request_connection() as c:  # a borrower that failed midway, inside a transaction
            pids.add(c.info.backend_pid)
            c.execute("SELECT 1")
            raise RuntimeError("boom")
    with db.request_connection() as c:  # direct: SET TRANSACTION first, on the returned connection
        pids.add(c.info.backend_pid)
        assert c.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
        c.execute(SNAPSHOT)
    for params in ({"limit": 5}, {}):  # and whole requests (edge checkout, then the handler's)
        resp = client.get(PATH, params={**span(1), **params})
        assert resp.status_code == 200 and len(resp.json()["lines"]) == 1
    with db.request_connection() as c:
        pids.add(c.info.backend_pid)
    assert len(pids) == 1  # it really was one connection throughout
