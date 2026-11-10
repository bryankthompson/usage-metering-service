import json
from datetime import datetime

import pytest

from app import reconcile
from app.rollup import close_hour

H = datetime.fromisoformat("2026-11-10T10:00:00+00:00")
HOUR = "2026-11-10T10:00:00Z"


def add_event(conn, key, when="2026-11-10T10:05:00Z", tenant="t1", model="mA", tokens_in=100, tokens_out=50, late=False):
    conn.execute(
        "INSERT INTO usage_events (tenant_id, idempotency_key, model, tokens_in, tokens_out, occurred_at, late)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (tenant, key, model, tokens_in, tokens_out, datetime.fromisoformat(when), late))
    conn.commit()


@pytest.fixture
def closed(conn):
    """Hour H closed with two events for t1/mA (200 in, 100 out) and one for t2/mA."""
    add_event(conn, "a")
    add_event(conn, "b")
    add_event(conn, "c", tenant="t2", tokens_in=7, tokens_out=3)
    close_hour(conn, H)
    return conn


def actuals_file(tmp_path, *rows):
    path = tmp_path / "actuals.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return str(path)


def actual(tenant="t1", model="mA", tokens_in=200, tokens_out=100, hour=HOUR):
    return {"tenant_id": tenant, "model": model, "hour": hour, "tokens_in": tokens_in, "tokens_out": tokens_out}


def reconcile_run(capsys, *argv):
    try:
        code = reconcile.main(list(argv))
    except SystemExit as e:  # argparse rejects a bad argument with exit 2
        code = e.code
    out = capsys.readouterr()
    return code, out.out, out.err


def test_clean_data_passes(closed, capsys):
    code, out, _ = reconcile_run(capsys)
    assert (code, out) == (0, "reconcile: hours=1 rows=2 drift=0\n")


def test_closed_hour_with_no_events_is_not_drift(conn, capsys):
    close_hour(conn, H)
    code, out, _ = reconcile_run(capsys)
    assert (code, out) == (0, "reconcile: hours=1 rows=0 drift=0\n")


def test_rollup_changed_by_hand_is_reported(closed, capsys):
    closed.execute("UPDATE usage_rollups_hourly SET tokens_out = 99, event_count = 3 WHERE tenant_id = 't1'")
    closed.commit()
    code, out, _ = reconcile_run(capsys)
    assert code == 1
    assert "tenant=t1 model=mA hour=2026-11-10T10:00:00Z tokens_out rollup=99 events=100; event_count rollup=3 events=2" in out
    assert out.endswith("hours=1 rows=2 drift=1\n")


def test_event_count_difference_alone_is_reported(closed, capsys):
    closed.execute("UPDATE usage_rollups_hourly SET event_count = 5 WHERE tenant_id = 't1'")
    closed.commit()
    code, out, _ = reconcile_run(capsys)
    assert code == 1 and "event_count rollup=5 events=2" in out


def test_missing_rollup_row_and_extra_row_are_reported(closed, capsys):
    closed.execute("DELETE FROM usage_rollups_hourly WHERE tenant_id = 't2'")
    closed.execute("INSERT INTO usage_rollups_hourly VALUES ('t3', 'mA', %s, 1, 1, 1)", (H,))
    closed.commit()
    code, out, _ = reconcile_run(capsys)
    assert code == 1
    assert "tenant=t2 model=mA hour=2026-11-10T10:00:00Z events but no rollup row" in out
    assert "tenant=t3 model=mA hour=2026-11-10T10:00:00Z rollup row but no events" in out
    assert out.endswith("drift=2\n")


def test_late_events_are_left_out_of_the_recompute(closed, capsys):
    add_event(closed, "late", tokens_in=5000, late=True)
    assert reconcile_run(capsys)[0] == 0


def test_events_outside_the_hour_are_not_counted(closed, capsys):
    add_event(closed, "next", when="2026-11-10T11:00:00Z")  # next hour, not closed
    add_event(closed, "before", when="2026-11-10T09:59:59Z")
    assert reconcile_run(capsys)[0] == 0


def test_hour_and_since_pick_the_hours(conn, capsys):
    add_event(conn, "a")
    add_event(conn, "b", when="2026-11-10T12:00:00Z")
    close_hour(conn, H)
    close_hour(conn, datetime.fromisoformat("2026-11-10T12:00:00+00:00"))
    conn.execute("UPDATE usage_rollups_hourly SET tokens_in = 1 WHERE hour_start = %s", (H,))
    conn.commit()
    assert reconcile_run(capsys, "--hour", HOUR)[0] == 1
    assert reconcile_run(capsys, "--hour", "2026-11-10T12:00:00Z")[0] == 0
    code, out, _ = reconcile_run(capsys, "--since", "2026-11-10T11:00:00Z")
    assert (code, out) == (0, "reconcile: hours=1 rows=1 drift=0\n")


def test_hour_that_is_not_closed_is_a_bad_argument(closed, capsys):
    assert reconcile_run(capsys, "--hour", "2026-11-10T13:00:00Z")[0] == 2


@pytest.mark.parametrize("argv", [["--hour", "2026-11-10T10:30:00Z"], ["--hour", "2026-11-10T10:00:00"],
                                  ["--since", "nope"], ["--tolerance-bps", "-1"], ["--tolerance-bps", "1.5"],
                                  ["--hour", HOUR, "--since", HOUR]])
def test_bad_arguments_exit_2(closed, capsys, argv):
    assert reconcile_run(capsys, *argv)[0] == 2


def test_actuals_within_tolerance_pass_and_the_exact_boundary_passes(closed, tmp_path, capsys):
    # 100 bps of 200 is 2 tokens; 100 bps of 100 is 1 token. Rollup is 200 in, 100 out.
    path = actuals_file(tmp_path, actual(tokens_in=202, tokens_out=101), actual(tenant="t2", tokens_in=7, tokens_out=3))
    code, out, _ = reconcile_run(capsys, "--actuals", path)
    assert (code, out) == (0, "reconcile: hours=1 rows=4 drift=0\n")


def test_actuals_exactly_on_the_boundary_pass_and_one_bp_tighter_flags(closed, tmp_path, capsys):
    # rollup 200 vs actual 400: the difference is exactly 5000 bps of the actual.
    path = actuals_file(tmp_path, actual(tokens_in=400), actual(tenant="t2", tokens_in=7, tokens_out=3))
    assert reconcile_run(capsys, "--actuals", path, "--tolerance-bps", "5000")[0] == 0
    assert reconcile_run(capsys, "--actuals", path, "--tolerance-bps", "4999")[0] == 1


def test_actuals_one_past_the_boundary_are_flagged(closed, tmp_path, capsys):
    path = actuals_file(tmp_path, actual(tokens_in=203, tokens_out=100), actual(tenant="t2", tokens_in=7, tokens_out=3))
    code, out, _ = reconcile_run(capsys, "--actuals", path)
    assert code == 1
    assert "tenant=t1 model=mA hour=2026-11-10T10:00:00Z beyond 100 bps: tokens_in rollup=200 actual=203 diff=-3" in out


def test_tolerance_option_moves_the_boundary(closed, tmp_path, capsys):
    path = actuals_file(tmp_path, actual(tokens_in=210), actual(tenant="t2", tokens_in=7, tokens_out=3))
    assert reconcile_run(capsys, "--actuals", path)[0] == 1
    assert reconcile_run(capsys, "--actuals", path, "--tolerance-bps", "500")[0] == 0  # 5% of 210 is 10.5
    assert reconcile_run(capsys, "--actuals", path, "--tolerance-bps", "476")[0] == 1  # 476 bps of 210 is 9.996


def test_actual_of_zero_allows_no_difference(closed, tmp_path, capsys):
    path = actuals_file(tmp_path, actual(tokens_in=0), actual(tenant="t2", tokens_in=7, tokens_out=3))
    assert reconcile_run(capsys, "--actuals", path, "--tolerance-bps", "10000")[0] == 1


def test_rows_on_one_side_only_are_flagged(closed, tmp_path, capsys):
    path = actuals_file(tmp_path, actual(), actual(tenant="t9", tokens_in=1, tokens_out=1))  # t2 has no actual
    code, out, _ = reconcile_run(capsys, "--actuals", path)
    assert code == 1
    assert "tenant=t9 model=mA hour=2026-11-10T10:00:00Z actual but no rollup row" in out
    assert "tenant=t2 model=mA hour=2026-11-10T10:00:00Z rollup row but no actual" in out


def test_actuals_for_hours_not_checked_are_skipped_and_said_so(closed, tmp_path, capsys):
    path = actuals_file(tmp_path, actual(), actual(tenant="t2", tokens_in=7, tokens_out=3), actual(hour="2026-11-10T15:00:00Z"))
    code, _, err = reconcile_run(capsys, "--actuals", path)
    assert code == 0
    assert "skipped 1 actuals rows" in err


@pytest.mark.parametrize("bad", ['{"tenant_id": "t1"', "[1]", json.dumps({**actual(), "extra": 1}),
                                 json.dumps(actual(tokens_in=1.5)), json.dumps(actual(tokens_in=-1)),
                                 json.dumps(actual(tokens_in=True)), json.dumps(actual(hour="2026-11-10T10:30:00Z")),
                                 json.dumps(actual(hour="2026-11-10T10:00:00"))])
def test_malformed_actuals_line_exits_2_and_names_the_line(closed, tmp_path, capsys, bad):
    path = tmp_path / "a.jsonl"
    path.write_text(json.dumps(actual(tenant="t2")) + "\n" + bad + "\n")
    code, out, err = reconcile_run(capsys, "--actuals", str(path))
    assert (code, out) == (2, "")
    assert "line 2" in err


def test_a_repeated_actuals_row_exits_2_and_names_its_line(closed, tmp_path, capsys):
    path = tmp_path / "a.jsonl"
    path.write_text(json.dumps(actual()) + "\n" + json.dumps(actual(tenant="t2")) + "\n" + json.dumps(actual()) + "\n")
    code, _, err = reconcile_run(capsys, "--actuals", str(path))
    assert code == 2 and "line 3" in err


def test_missing_actuals_file_exits_2(closed, tmp_path, capsys):
    assert reconcile_run(capsys, "--actuals", str(tmp_path / "none.jsonl"))[0] == 2


def test_reconcile_writes_nothing(closed, capsys):
    before = closed.execute("SELECT (SELECT count(*) FROM usage_events) AS e, (SELECT count(*) FROM usage_rollups_hourly) AS r,"
                            " (SELECT count(*) FROM closed_hours) AS c").fetchone()
    reconcile_run(capsys)
    closed.rollback()
    assert closed.execute("SELECT (SELECT count(*) FROM usage_events) AS e, (SELECT count(*) FROM usage_rollups_hourly) AS r,"
                          " (SELECT count(*) FROM closed_hours) AS c").fetchone() == before
