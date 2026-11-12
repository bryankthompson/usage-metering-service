"""DUE_HOURS must name the same hours as the original query, and must not read every event to do it.

OLD_DUE_HOURS is the query as it was before it was made sargable (a full scan of usage_events). It is the oracle: any
difference from it means the rollup would close a different set of hours, which is the money path.
"""

import json
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from app import db, pricing, rollup
from tests.test_rollup import add_event, at

OLD_DUE_HOURS = """
SELECT DISTINCT date_trunc('hour', e.occurred_at, 'UTC') AS hour_start
FROM usage_events e
WHERE date_trunc('hour', e.occurred_at, 'UTC') + interval '1 hour' <= %(cutoff)s
  AND NOT EXISTS (SELECT 1 FROM closed_hours c WHERE c.hour_start = date_trunc('hour', e.occurred_at, 'UTC'))
ORDER BY hour_start
"""
MICRO = timedelta(microseconds=1)


def due(conn, sql, cutoff):
    rows = conn.execute(sql, {"cutoff": cutoff}).fetchall()
    conn.commit()
    return [r["hour_start"] for r in rows]


def close(conn, *hours):
    for h in hours:
        conn.execute("INSERT INTO closed_hours (hour_start) VALUES (%s)", (h,))
    conn.commit()


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


def cutoffs_around(*instants):
    """Every instant, one microsecond before and one after: the places where <= and < differ."""
    return sorted({i + d for i in instants for d in (-MICRO, timedelta(0), MICRO)})


def assert_same_as_old(conn, cutoffs):
    for cutoff in cutoffs:
        new, old = due(conn, rollup.DUE_HOURS, cutoff), due(conn, OLD_DUE_HOURS, cutoff)
        assert new == old, f"cutoff {cutoff.isoformat()}: new {new} != old {old}"
        assert all(
            h.tzinfo is not None and h == h.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
            for h in new
        )
        assert new == sorted(set(new))


def test_the_oracle_and_the_new_query_agree_on_an_empty_table(conn):
    assert due(conn, rollup.DUE_HOURS, utc(2030, 1, 1)) == []
    assert due(conn, OLD_DUE_HOURS, utc(2030, 1, 1)) == []


def test_events_on_and_around_an_hour_boundary(conn):
    # 09:59:59.999999 is hour 9; 10:00:00 and 10:00:00.000001 are hour 10; 11:00:00 is hour 11
    for i, when in enumerate(
        ["2026-11-10T09:59:59.999999Z", "2026-11-10T10:00:00Z", "2026-11-10T10:00:00.000001Z", "2026-11-10T11:00:00Z"]
    ):
        add_event(conn, f"b{i}", when)
    assert due(conn, OLD_DUE_HOURS, utc(2026, 11, 10, 12)) == [
        utc(2026, 11, 10, 9),
        utc(2026, 11, 10, 10),
        utc(2026, 11, 10, 11),
    ]
    assert_same_as_old(conn, cutoffs_around(utc(2026, 11, 10, 10), utc(2026, 11, 10, 11), utc(2026, 11, 10, 12)))


def test_the_cutoff_exactly_at_the_end_of_an_hour_makes_it_due_and_one_microsecond_less_does_not(conn):
    add_event(conn, "c1", "2026-11-10T10:30:00Z")
    end = utc(2026, 11, 10, 11)
    assert due(conn, rollup.DUE_HOURS, end - MICRO) == []
    assert due(conn, rollup.DUE_HOURS, end) == [utc(2026, 11, 10, 10)]
    assert due(conn, rollup.DUE_HOURS, end + MICRO) == [utc(2026, 11, 10, 10)]
    assert_same_as_old(conn, cutoffs_around(end))


def test_offsets_that_are_not_utc_land_in_their_utc_hour(conn):
    add_event(conn, "o1", "2026-11-10T15:30:00+05:30")  # 10:00 UTC
    add_event(conn, "o2", "2026-11-10T06:59:59.999999-05:00")  # 11:59:59.999999 UTC
    add_event(conn, "o3", "2026-11-10T23:00:00+13:00")  # 10:00 UTC, a whole-hour offset
    assert due(conn, rollup.DUE_HOURS, utc(2030, 1, 1)) == [utc(2026, 11, 10, 10), utc(2026, 11, 10, 11)]
    assert_same_as_old(conn, cutoffs_around(utc(2026, 11, 10, 11), utc(2026, 11, 10, 12)))


def test_closed_hours_are_left_out_and_late_events_do_not_reopen_them(conn):
    add_event(conn, "l1", "2026-11-10T08:10:00Z")
    add_event(conn, "l2", "2026-11-10T09:10:00Z")
    add_event(conn, "l3", "2026-11-10T09:20:00Z", late=True)  # late: its hour was already closed when it arrived
    add_event(conn, "l4", "2026-11-10T10:10:00Z")
    close(conn, utc(2026, 11, 10, 9))
    assert due(conn, rollup.DUE_HOURS, utc(2030, 1, 1)) == [utc(2026, 11, 10, 8), utc(2026, 11, 10, 10)]
    assert_same_as_old(conn, cutoffs_around(utc(2026, 11, 10, 9), utc(2026, 11, 10, 11)))


def test_an_hour_with_only_late_events_that_is_not_closed_is_still_due(conn):
    # Cannot happen through ingest, but the old query did not look at `late` and neither may the new one.
    add_event(conn, "x1", "2026-11-10T08:10:00Z", late=True)
    assert due(conn, rollup.DUE_HOURS, utc(2030, 1, 1)) == [utc(2026, 11, 10, 8)]
    assert_same_as_old(conn, [utc(2030, 1, 1)])


def test_hours_closed_out_of_order_with_an_open_hour_between_them(conn):
    for h in range(6, 14):
        add_event(conn, f"ooo{h}", f"2026-11-10T{h:02d}:15:00Z")
    close(conn, utc(2026, 11, 10, 6), utc(2026, 11, 10, 7), utc(2026, 11, 10, 11), utc(2026, 11, 10, 13))
    expected = [utc(2026, 11, 10, h) for h in (8, 9, 10, 12)]
    assert due(conn, rollup.DUE_HOURS, utc(2030, 1, 1)) == expected
    assert_same_as_old(conn, cutoffs_around(*(utc(2026, 11, 10, h) for h in range(6, 15))))


def test_hours_with_one_event_and_with_many_and_a_gap_of_empty_hours(conn):
    add_event(conn, "g1", "2026-11-01T00:00:00Z")
    for i in range(25):
        add_event(conn, f"m{i}", f"2026-11-05T12:{i:02d}:00Z")
    add_event(conn, "g2", "2026-11-09T23:59:59.999999Z")
    expected = [utc(2026, 11, 1, 0), utc(2026, 11, 5, 12), utc(2026, 11, 9, 23)]
    assert due(conn, rollup.DUE_HOURS, utc(2030, 1, 1)) == expected
    assert_same_as_old(conn, cutoffs_around(utc(2026, 11, 1, 1), utc(2026, 11, 5, 13), utc(2026, 11, 10)))


def test_two_tenants_share_one_hour_and_count_it_once(conn):
    add_event(conn, "t1a", "2026-11-10T10:05:00Z", tenant="t1")
    add_event(conn, "t2a", "2026-11-10T10:06:00Z", tenant="t2")
    add_event(conn, "t2b", "2026-11-10T12:06:00Z", tenant="t2")
    assert due(conn, rollup.DUE_HOURS, utc(2030, 1, 1)) == [utc(2026, 11, 10, 10), utc(2026, 11, 10, 12)]
    assert_same_as_old(conn, cutoffs_around(utc(2026, 11, 10, 11), utc(2026, 11, 10, 13)))


def test_year_one_and_a_far_future_event(conn):
    add_event(conn, "y1", "0001-01-01T00:00:00Z")
    add_event(conn, "y2", "0001-01-01T00:59:59.999999Z")
    add_event(conn, "now", "2026-11-10T10:00:00Z")
    add_event(conn, "far1", "9999-12-31T22:00:00Z")
    add_event(conn, "far2", "9999-12-31T23:59:59.999999Z")  # its hour ends in year 10000: never due, and no error
    first_two = [utc(1, 1, 1, 0), utc(2026, 11, 10, 10)]
    assert due(conn, rollup.DUE_HOURS, utc(2026, 11, 11)) == first_two
    assert due(conn, rollup.DUE_HOURS, utc(9999, 12, 31, 22, 59, 59, 999999)) == first_two
    assert due(conn, rollup.DUE_HOURS, utc(9999, 12, 31, 23)) == first_two + [utc(9999, 12, 31, 22)]
    assert due(conn, rollup.DUE_HOURS, datetime.max.replace(tzinfo=timezone.utc)) == first_two + [utc(9999, 12, 31, 22)]
    assert_same_as_old(conn, cutoffs_around(utc(1, 1, 1, 1), utc(2026, 11, 10, 11), utc(9999, 12, 31, 23)))


def test_the_closed_hour_of_the_first_and_of_the_last_event(conn):
    for h in (3, 4, 5):
        add_event(conn, f"e{h}", f"2026-11-10T0{h}:30:00Z")
    close(conn, utc(2026, 11, 10, 3), utc(2026, 11, 10, 5))
    assert due(conn, rollup.DUE_HOURS, utc(2030, 1, 1)) == [utc(2026, 11, 10, 4)]
    assert_same_as_old(conn, cutoffs_around(utc(2026, 11, 10, 4), utc(2026, 11, 10, 5), utc(2026, 11, 10, 6)))


def test_close_due_hours_closes_the_same_hours_for_any_now_and_grace(conn):
    for h in range(6, 12):
        add_event(conn, f"cd{h}", f"2026-11-10T{h:02d}:20:00Z")
    close(conn, utc(2026, 11, 10, 8))
    now = at("2026-11-10T10:05:00+00:00")
    for grace in (timedelta(0), rollup.DEFAULT_GRACE, timedelta(hours=1), timedelta(hours=3)):
        want = due(conn, OLD_DUE_HOURS, now - grace)
        conn.rollback()
        closed = rollup.close_due_hours(conn, now, grace=grace)
        assert [h for h, _, _ in closed] == want, f"grace {grace}"
        conn.execute("DELETE FROM closed_hours WHERE hour_start <> %s", (utc(2026, 11, 10, 8),))
        conn.execute("DELETE FROM usage_rollups_hourly")
        conn.commit()


def due_text(conn, sql, cutoff):
    """The hours as text, so an infinity row can be compared (psycopg refuses to load one as a datetime)."""
    rows = conn.execute(
        f"SELECT hour_start::text AS t FROM ({sql}) q ORDER BY hour_start", {"cutoff": cutoff}
    ).fetchall()
    conn.commit()
    return [r["t"] for r in rows]


def add_raw_event(conn, key, occurred_at_sql):
    """Straight SQL, as an operator could do it: the API refuses these, the schema does not."""
    conn.execute(
        "INSERT INTO usage_events (tenant_id, idempotency_key, model, tokens_in, tokens_out, occurred_at)"
        f" VALUES ('t1', %s, 'mA', 1, 1, '{occurred_at_sql}')",
        (key,),
    )
    conn.commit()


def with_short_timeout(conn):
    """A query that never ends fails this test (QueryCanceled) instead of hanging the suite."""
    conn.execute("SET statement_timeout = '5s'")
    conn.commit()


def test_a_plus_infinity_event_is_ignored_and_the_query_ends(conn):
    with_short_timeout(conn)
    add_event(conn, "n1", "2026-11-10T08:30:00Z")
    add_event(conn, "n2", "2026-11-10T10:30:00Z")
    add_raw_event(conn, "inf", "infinity")
    cutoff = utc(2030, 1, 1)
    assert due_text(conn, rollup.DUE_HOURS, cutoff) == ["2026-11-10 08:00:00+00", "2026-11-10 10:00:00+00"]
    assert due_text(conn, rollup.DUE_HOURS, cutoff) == due_text(conn, OLD_DUE_HOURS, cutoff)  # as before: never due


def test_a_minus_infinity_event_is_still_returned_so_the_close_fails_loudly_as_before(conn):
    with_short_timeout(conn)
    add_event(conn, "n1", "2026-11-10T08:30:00Z")
    add_raw_event(conn, "ninf", "-infinity")
    cutoff = utc(2030, 1, 1)
    assert due_text(conn, rollup.DUE_HOURS, cutoff) == ["-infinity", "2026-11-10 08:00:00+00"]
    assert due_text(conn, rollup.DUE_HOURS, cutoff) == due_text(conn, OLD_DUE_HOURS, cutoff)
    with pytest.raises(psycopg.DataError):  # psycopg cannot load '-infinity' as a datetime: the run stops with an error
        rollup.close_due_hours(conn, cutoff)
    conn.rollback()


def test_both_infinities_next_to_normal_events_end_too(conn):
    with_short_timeout(conn)
    add_event(conn, "n1", "2026-11-10T08:30:00Z")
    add_raw_event(conn, "ninf", "-infinity")
    add_raw_event(conn, "inf", "infinity")
    assert due_text(conn, rollup.DUE_HOURS, utc(2030, 1, 1)) == ["-infinity", "2026-11-10 08:00:00+00"]


# --- the plan: DUE_HOURS must cost per hour, not per event -------------------------------------------------------------

HOURS, PER_HOUR = 300, 200  # 60,000 events: the old plan reads all of them, the new one one per hour


def seed_many(conn, tenants=20):
    conn.execute(
        "INSERT INTO usage_events (tenant_id, idempotency_key, model, tokens_in, tokens_out, occurred_at)"
        f" SELECT 'tenant-' || mod(g, {tenants}), 'k' || g, 'm', 1, 1,"
        f" timestamptz '2026-01-01 00:00:00+00' + (g / {PER_HOUR}) * interval '1 hour' + mod(g, {PER_HOUR}) * interval '10 seconds'"
        f" FROM generate_series(0, {HOURS * PER_HOUR - 1}) g"
    )
    # every 50th hour is left open
    conn.execute(
        "INSERT INTO closed_hours (hour_start) SELECT timestamptz '2026-01-01 00:00:00+00' + g * interval '1 hour'"
        f" FROM generate_series(0, {HOURS - 1}) g WHERE mod(g, 50) <> 7"
    )
    conn.commit()
    conn.execute("ANALYZE usage_events")
    conn.execute("ANALYZE closed_hours")
    conn.commit()


def plan_nodes(conn, sql, params):
    """Every node of the executed plan, as dicts (EXPLAIN ANALYZE runs the statement)."""
    conn.execute("SET max_parallel_workers_per_gather = 0")  # one process, so 'Actual Rows' is the whole count
    raw = conn.execute("EXPLAIN (ANALYZE, FORMAT JSON) " + sql, params).fetchone()["QUERY PLAN"]
    conn.commit()
    plan = raw if isinstance(raw, list) else json.loads(raw)
    nodes, todo = [], [plan[0]["Plan"]]
    while todo:
        node = todo.pop()
        nodes.append(node)
        todo.extend(node.get("Plans", []))
    return nodes


def events_read(nodes):
    """Rows the plan pulled out of usage_events, over all loops."""
    return sum(n["Actual Rows"] * n["Actual Loops"] for n in nodes if n.get("Relation Name") == "usage_events")


def test_due_hours_does_not_read_every_event(conn):
    seed_many(conn)
    cutoff = utc(2027, 1, 1)
    nodes = plan_nodes(conn, rollup.DUE_HOURS, {"cutoff": cutoff})
    assert not [n for n in nodes if "Seq Scan" in n["Node Type"] and n.get("Relation Name") == "usage_events"]
    read = events_read(nodes)
    assert read <= 2 * HOURS, f"read {read} rows of usage_events for {HOURS} hours of {PER_HOUR} events"
    assert len(due(conn, rollup.DUE_HOURS, cutoff)) == len(range(7, HOURS, 50))  # and it still found the open hours
    assert due(conn, rollup.DUE_HOURS, cutoff) == due(conn, OLD_DUE_HOURS, cutoff)
    # the guard itself: the old query reads every event, so the same measure sees it
    assert events_read(plan_nodes(conn, OLD_DUE_HOURS, {"cutoff": cutoff})) == HOURS * PER_HOUR


def test_unclosed_hours_for_one_tenant_uses_the_tenant_index(conn):
    seed_many(conn)
    params = {"tenant": "tenant-3", "start": utc(2026, 1, 3), "end": utc(2026, 1, 8), "limit": 100}
    nodes = plan_nodes(conn, pricing.UNCLOSED_HOURS, params)
    assert "usage_events_tenant_occurred_at" in {n.get("Index Name") for n in nodes}, [n["Node Type"] for n in nodes]
    assert not [n for n in nodes if "Seq Scan" in n["Node Type"] and n.get("Relation Name") == "usage_events"]


# --- the indexes ---------------------------------------------------------------------------------------------------------


def usage_event_indexes(conn):
    rows = conn.execute(
        "SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'usage_events' ORDER BY indexname"
    ).fetchall()
    conn.commit()
    return {r["indexname"]: r["indexdef"] for r in rows}


def test_the_tenant_and_time_index_exists_and_the_time_index_is_kept(conn):
    indexes = usage_event_indexes(conn)
    assert indexes["usage_events_tenant_occurred_at"].endswith("USING btree (tenant_id, occurred_at)")
    assert indexes["usage_events_occurred_at"].endswith(
        "USING btree (occurred_at)"
    )  # AGGREGATE_HOUR and DUE_HOURS use it


def test_applying_the_schema_again_changes_no_index(conn):
    before = usage_event_indexes(conn)
    db.apply_schema(conn)
    db.apply_schema(conn)
    assert usage_event_indexes(conn) == before
