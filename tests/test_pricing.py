from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from app.pricing import NoPrice, add_price_version, charge_lines, price_at, rate

T0 = datetime(2026, 11, 1, 0, 0, tzinfo=timezone.utc)
HOUR = timedelta(hours=1)


def add_rollup(conn, tenant, model, hour_start, tokens_in, tokens_out):
    conn.execute(
        "INSERT INTO usage_rollups_hourly (tenant_id, model, hour_start, tokens_in, tokens_out, event_count)"
        " VALUES (%s, %s, %s, %s, %s, 1)", (tenant, model, hour_start, tokens_in, tokens_out))
    conn.commit()


def test_versions_count_up_per_model(conn):
    assert add_price_version(conn, "m1", T0, 1000, 2000) == 1
    assert add_price_version(conn, "m1", T0 + HOUR, 1100, 2100) == 2
    assert add_price_version(conn, "m2", T0, 5, 6) == 1


def test_naive_datetime_is_rejected(conn):
    with pytest.raises(ValueError):
        add_price_version(conn, "m1", datetime(2026, 11, 1), 1, 1)
    with pytest.raises(ValueError):
        price_at(conn, "m1", datetime(2026, 11, 1))


def test_price_at_picks_version_across_the_effective_boundary(conn):
    add_price_version(conn, "m1", T0, 1000, 2000)
    add_price_version(conn, "m1", T0 + 2 * HOUR, 3000, 4000)
    assert price_at(conn, "m1", T0)["version"] == 1                              # exactly at the first start
    assert price_at(conn, "m1", T0 + 2 * HOUR - timedelta(microseconds=1))["version"] == 1
    assert price_at(conn, "m1", T0 + 2 * HOUR)["version"] == 2                   # the boundary belongs to the new price
    assert price_at(conn, "m1", T0 + 30 * HOUR)["input_micros_per_1k"] == 3000


def test_same_effective_time_goes_to_the_later_version(conn):
    add_price_version(conn, "m1", T0, 1000, 2000)
    add_price_version(conn, "m1", T0, 1500, 2500)
    assert price_at(conn, "m1", T0)["version"] == 2


def test_no_price_raises_never_zero(conn):
    with pytest.raises(NoPrice):
        price_at(conn, "unknown", T0)                                            # model never priced
    add_price_version(conn, "m1", T0, 1000, 2000)
    with pytest.raises(NoPrice):
        rate(conn, "m1", 10, 10, T0 - timedelta(seconds=1))                      # before the first version


def test_rate_exact_values(conn):
    add_price_version(conn, "m1", T0, 1000, 3000)
    assert rate(conn, "m1", 1000, 0, T0) == 1000
    assert rate(conn, "m1", 0, 1000, T0) == 3000
    assert rate(conn, "m1", 2500, 400, T0) == 2500 + 1200
    assert rate(conn, "m1", 0, 0, T0) == 0


def test_rate_rounds_half_up(conn):
    add_price_version(conn, "half", T0, 1500, 1500)
    assert rate(conn, "half", 1, 0, T0) == 2           # 1.5 -> 2
    add_price_version(conn, "below", T0, 1499, 0)
    assert rate(conn, "below", 1, 0, T0) == 1          # 1.499 -> 1
    add_price_version(conn, "tiny", T0, 499, 0)
    assert rate(conn, "tiny", 1, 0, T0) == 0           # 0.499 -> 0, a real zero only because the price says so


def test_rate_rounds_once_on_the_sum(conn):
    # 0.5 + 0.5 = 1.0 exactly. Rounding each side first would give 1 + 1 = 2.
    add_price_version(conn, "m1", T0, 500, 500)
    assert rate(conn, "m1", 1, 1, T0) == 1
    # 0.4 + 0.4 = 0.8 -> 1. Rounding each side first would give 0 + 0 = 0.
    add_price_version(conn, "m2", T0, 400, 400)
    assert rate(conn, "m2", 1, 1, T0) == 1


def test_rate_is_exact_beyond_float_precision(conn):
    add_price_version(conn, "m1", T0, 3_000_001, 0)
    result = rate(conn, "m1", 10**12, 0, T0)
    assert result == 3_000_001_000_000_000
    assert type(result) is int


def test_rate_rejects_negative_tokens(conn):
    add_price_version(conn, "m1", T0, 1000, 1000)
    with pytest.raises(ValueError):
        rate(conn, "m1", -1, 0, T0)


def test_stored_versions_cannot_change(conn):
    add_price_version(conn, "m1", T0, 1000, 2000)
    with pytest.raises(psycopg.errors.RaiseException):
        conn.execute("UPDATE price_versions SET input_micros_per_1k = 1")
    conn.rollback()
    with pytest.raises(psycopg.errors.RaiseException):
        conn.execute("DELETE FROM price_versions")
    conn.rollback()
    assert price_at(conn, "m1", T0)["input_micros_per_1k"] == 1000


def test_charge_lines_apply_a_mid_period_price_change_from_its_effective_time(conn):
    add_price_version(conn, "m1", T0, 1000, 2000)
    add_price_version(conn, "m1", T0 + 2 * HOUR, 3000, 4000)
    for n in range(4):                                                           # hours 0, 1 at v1; hours 2, 3 at v2
        add_rollup(conn, "t1", "m1", T0 + n * HOUR, 1000, 1000)
    lines, total = charge_lines(conn, "t1", T0, T0 + 4 * HOUR)
    assert [(line["price_version"], line["micros"]) for line in lines] == [(1, 3000), (1, 3000), (2, 7000), (2, 7000)]
    assert total == 20_000
    assert all(type(line["micros"]) is int for line in lines) and type(total) is int


def test_charge_lines_window_is_half_open_and_per_tenant(conn):
    add_price_version(conn, "m1", T0, 1000, 1000)
    add_rollup(conn, "t1", "m1", T0, 1000, 0)
    add_rollup(conn, "t1", "m1", T0 + HOUR, 1000, 0)
    add_rollup(conn, "t2", "m1", T0, 9000, 0)                                    # another tenant: never counted
    lines, total = charge_lines(conn, "t1", T0, T0 + HOUR)                       # end is excluded
    assert [line["hour_start"] for line in lines] == [T0]
    assert total == 1000


def test_charge_lines_for_an_empty_window_is_zero_lines(conn):
    assert charge_lines(conn, "t1", T0, T0 + HOUR) == ([], 0)


def test_charge_lines_without_a_price_raises(conn):
    add_price_version(conn, "m1", T0 + HOUR, 1000, 1000)
    add_rollup(conn, "t1", "m1", T0, 1000, 0)                                    # the hour starts before any price
    with pytest.raises(NoPrice):
        charge_lines(conn, "t1", T0, T0 + 2 * HOUR)


def test_charge_lines_round_each_line_then_sum(conn):
    add_price_version(conn, "m1", T0, 1500, 0)
    add_rollup(conn, "t1", "m1", T0, 1, 0)
    add_rollup(conn, "t1", "m1", T0 + HOUR, 1, 0)
    lines, total = charge_lines(conn, "t1", T0, T0 + 2 * HOUR)
    assert [line["micros"] for line in lines] == [2, 2]
    assert total == 4
