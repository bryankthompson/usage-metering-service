"""The ClickHouse mirror is off the rollup's critical path: `close_due_hours` closes every due hour first and mirrors
them in one pass after the loop, a down ClickHouse is paid for once per pass, and `backfill` groups rows once by hour."""
import logging
import urllib.error
from datetime import datetime, timedelta, timezone

import pytest

from app import mirror, rollup
from app.rollup import close_due_hours, close_hour
from tests.test_rollup import add_event, at, closed_hours, rollups

NOW = at("2026-11-10T12:00:00Z")
HOURS = [datetime(2026, 11, 10, h, tzinfo=timezone.utc) for h in range(5, 10)]  # 5 due hours


@pytest.fixture
def five_hours(conn):
    for h in HOURS:
        add_event(conn, f"e{h.hour}", h.replace(minute=30).isoformat(), tokens_in=h.hour)


class Calls:
    """Replaces mirror.write_rows. Records, per call, the hour and how many hours were closed at that moment."""

    def __init__(self, conn, fail=None):
        self.conn, self.fail, self.calls, self.sent = conn, fail or {}, [], {}

    def __call__(self, url, rows):
        hour = rows[0]["hour_start"]
        self.calls.append((hour, len(closed_hours(self.conn))))
        if hour in self.fail:
            raise self.fail[hour]
        self.sent[hour] = rows


@pytest.fixture
def url(monkeypatch):
    monkeypatch.setenv("CLICKHOUSE_URL", "http://clickhouse.invalid:8123")


def test_every_hour_is_closed_before_the_first_mirror_call_and_all_are_mirrored(conn, url, five_hours, monkeypatch):
    calls = Calls(conn)
    monkeypatch.setattr(mirror, "write_rows", calls)
    closed = close_due_hours(conn, NOW)
    assert [h for h, _, _ in closed] == HOURS
    assert [h for h, _ in calls.calls] == HOURS                      # oldest first, each once
    assert {n for _, n in calls.calls} == {5}                        # all 5 commits happened before the first mirror call
    assert [r["tokens_in"] for h in HOURS for r in calls.sent[h]] == [h.hour for h in HOURS]


def test_a_mirror_that_is_down_is_tried_once_and_the_other_hours_are_logged_skipped(conn, url, five_hours, monkeypatch, caplog):
    calls = Calls(conn, fail={h: urllib.error.URLError("refused") for h in HOURS})
    monkeypatch.setattr(mirror, "write_rows", calls)
    with caplog.at_level(logging.ERROR):
        closed = close_due_hours(conn, NOW)
    assert len(closed) == 5 and closed_hours(conn) == HOURS and len(rollups(conn)) == 5
    assert calls.calls == [(HOURS[0], 5)]                            # one attempt
    assert f"clickhouse mirror failed: hour={HOURS[0].isoformat()}" in caplog.text
    for h in HOURS[1:]:
        assert caplog.text.count(f"clickhouse mirror skipped: hour={h.isoformat()} after an earlier failure in this pass") == 1
    assert "skipped: hour=" + HOURS[0].isoformat() not in caplog.text


@pytest.mark.parametrize("err", [TimeoutError("timed out"), ConnectionRefusedError(), OSError("no route")])
def test_a_timeout_or_oserror_also_skips_the_rest(conn, url, five_hours, monkeypatch, err):
    calls = Calls(conn, fail={HOURS[1]: err})
    monkeypatch.setattr(mirror, "write_rows", calls)
    close_due_hours(conn, NOW)
    assert [h for h, _ in calls.calls] == HOURS[:2]                  # hour 1 ok, hour 2 fails, hours 3-5 skipped


def test_an_http_error_for_one_hour_does_not_stop_the_others(conn, url, five_hours, monkeypatch, caplog):
    calls = Calls(conn, fail={HOURS[1]: RuntimeError("clickhouse answered 500: boom")})
    monkeypatch.setattr(mirror, "write_rows", calls)
    with caplog.at_level(logging.ERROR):
        closed = close_due_hours(conn, NOW)
    assert len(closed) == 5
    assert [h for h, _ in calls.calls] == HOURS                      # all five tried
    assert set(calls.sent) == set(HOURS) - {HOURS[1]}
    assert "skipped" not in caplog.text and f"mirror failed: hour={HOURS[1].isoformat()}" in caplog.text


def test_a_bad_hour_value_error_does_not_stop_the_others(conn, url, five_hours, monkeypatch):
    calls = Calls(conn, fail={HOURS[0]: ValueError("outside range")})
    monkeypatch.setattr(mirror, "write_rows", calls)
    close_due_hours(conn, NOW)
    assert [h for h, _ in calls.calls] == HOURS


def test_a_failed_hour_is_not_mirrored_and_does_not_stop_the_pass(conn, url, five_hours, monkeypatch):
    real = close_hour

    def flaky(conn, hour, **kw):
        if hour == HOURS[2]:
            raise RuntimeError("boom")
        return real(conn, hour, **kw)

    monkeypatch.setattr(rollup, "close_hour", flaky)
    calls = Calls(conn)
    monkeypatch.setattr(mirror, "write_rows", calls)
    failed = []
    closed = close_due_hours(conn, NOW, failed=failed)
    assert failed == [HOURS[2]] and len(closed) == 4
    assert [h for h, _ in calls.calls] == [h for h in HOURS if h != HOURS[2]]


def test_a_rollup_closes_and_reports_the_same_whether_the_mirror_is_up_or_down(conn, url, five_hours, monkeypatch):
    monkeypatch.setattr(mirror, "write_rows", Calls(conn))
    up = close_due_hours(conn, NOW)
    rows_up = rollups(conn)
    conn.execute("DELETE FROM usage_rollups_hourly")
    conn.execute("DELETE FROM closed_hours")
    conn.commit()
    monkeypatch.setattr(mirror, "write_rows", Calls(conn, fail={h: OSError("down") for h in HOURS}))
    down = close_due_hours(conn, NOW)
    assert down == up and rollups(conn) == rows_up


def test_without_clickhouse_url_the_pass_calls_nothing(conn, five_hours, monkeypatch):
    monkeypatch.delenv("CLICKHOUSE_URL", raising=False)
    calls = Calls(conn)
    monkeypatch.setattr(mirror, "write_rows", calls)
    assert len(close_due_hours(conn, NOW)) == 5
    assert calls.calls == []


def test_a_direct_close_hour_still_mirrors_after_its_commit(conn, url, five_hours, monkeypatch):
    calls = Calls(conn)
    monkeypatch.setattr(mirror, "write_rows", calls)
    assert close_hour(conn, HOURS[0]) == (1, 1)
    assert calls.calls == [(HOURS[0], 1)]                            # the commit was already there


def test_run_once_output_is_the_same_with_the_mirror_down(conn, url, five_hours, monkeypatch, capsys):
    monkeypatch.setattr(mirror, "write_rows", Calls(conn, fail={h: OSError("down") for h in HOURS}))
    monkeypatch.setattr(rollup, "datetime", type("D", (), {"now": staticmethod(lambda tz=None: NOW)}))
    assert rollup.run_once() == []
    out = capsys.readouterr().out.splitlines()
    assert out == [f"closed hour={h.isoformat()} rows=1 events=1" for h in HOURS]


# ---- P1-11: backfill groups once ----

def old_backfill(rows, mirrored):
    """The grouping of backfill on main (e1baa59), as the oracle: the hours it would send, with the rows of each."""
    by_hour = {}
    for row in rows:
        if mirrored.get(mirror._row_key(row)) != mirror._row_values(row):
            by_hour.setdefault(mirror._utc(row["hour_start"]), []).append(row)
    return [(hour, [r for r in rows if mirror._utc(r["hour_start"]) == hour]) for hour, _ in sorted(by_hour.items())]


class FakeConn:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, query):
        return self

    def fetchall(self):
        return self.rows

    def commit(self):
        pass


def generated(n_hours, per_hour):
    """Rows in tz-aware hours of mixed offsets, plus the mirror's state: some hours missing, some half different."""
    offsets = [timezone.utc, timezone(timedelta(hours=5, minutes=30)), timezone(timedelta(hours=-8))]
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows, mirrored = [], {}
    for i in range(n_hours):
        hour = (base + timedelta(hours=i)).astimezone(offsets[i % 3])
        for j in range(per_hour):
            row = {"tenant_id": f"t{j}", "model": "m", "hour_start": hour, "tokens_in": i, "tokens_out": j, "event_count": 1}
            rows.append(row)
            if i % 4 != 0:                                           # every 4th hour missing in the mirror
                values = mirror._row_values(row)
                if i % 4 == 1 and j == 0:
                    values = (values[0] + 1, values[1], values[2])   # one row different
                mirrored[mirror._row_key(row)] = values
    mirrored[("tx", "m", base - timedelta(hours=1))] = (1, 1, 1)     # ClickHouse-only row
    return rows, mirrored


def run_new(monkeypatch, rows, mirrored):
    sent = []
    monkeypatch.setattr(mirror, "read_mirror", lambda url: mirrored)
    monkeypatch.setattr(mirror, "write_rows", lambda url, r: sent.append(r))
    result = mirror.backfill(FakeConn(rows), "u")
    return result, sent


def test_new_backfill_sends_what_the_old_grouping_sent(monkeypatch):
    rows, mirrored = generated(40, 3)
    result, sent = run_new(monkeypatch, rows, mirrored)
    expected = old_backfill(rows, mirrored)
    assert len(expected) == 20                                       # 10 missing hours, 10 with one row different, 20 identical
    assert result == (len(expected), [])
    assert [(mirror._utc(r[0]["hour_start"]), r) for r in sent] == expected


def test_new_backfill_counts_a_failed_hour_in_the_same_slot(monkeypatch):
    rows, mirrored = generated(8, 2)
    monkeypatch.setattr(mirror, "read_mirror", lambda url: mirrored)

    def write(url, r):
        if mirror._utc(r[0]["hour_start"]) == datetime(2026, 1, 1, 4, tzinfo=timezone.utc):
            raise RuntimeError("no")

    monkeypatch.setattr(mirror, "write_rows", write)
    assert mirror.backfill(FakeConn(rows), "u") == (3, [datetime(2026, 1, 1, 4, tzinfo=timezone.utc)])


def test_backfill_work_is_linear_in_rows(monkeypatch):
    rows, mirrored = generated(5000, 20)
    calls = 0
    real = mirror._utc

    def counting(hour):
        nonlocal calls
        calls += 1
        return real(hour)

    monkeypatch.setattr(mirror, "_utc", counting)
    result, sent = run_new(monkeypatch, rows, mirrored)
    assert result[0] == len(sent) and result[0] > 0
    assert calls < 5 * len(rows)                                     # old code: ~ hours x rows = 5000 x 100,000
