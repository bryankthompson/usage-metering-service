import json
from datetime import datetime

import pytest

from app import reconcile
from app.rollup import close_hour

H = datetime.fromisoformat("2026-11-10T10:00:00+00:00")
HOUR = "2026-11-10T10:00:00Z"


def add_event(
    conn, key, when="2026-11-10T10:05:00Z", tenant="t1", model="mA", tokens_in=100, tokens_out=50, late=False
):
    conn.execute(
        "INSERT INTO usage_events (tenant_id, idempotency_key, model, tokens_in, tokens_out, occurred_at, late)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (tenant, key, model, tokens_in, tokens_out, datetime.fromisoformat(when), late),
    )
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
    assert code == 0 and out.endswith("reconcile: hours=1 rows=2 drift=0\n")


def test_closed_hour_with_no_events_is_not_drift(conn, capsys):
    close_hour(conn, H)
    code, out, _ = reconcile_run(capsys)
    assert code == 0 and out.endswith("reconcile: hours=1 rows=0 drift=0\n")


def test_rollup_changed_by_hand_is_reported(closed, capsys):
    closed.execute("UPDATE usage_rollups_hourly SET tokens_out = 99, event_count = 3 WHERE tenant_id = 't1'")
    closed.commit()
    code, out, _ = reconcile_run(capsys)
    assert code == 1
    assert (
        "tenant=t1 model=mA hour=2026-11-10T10:00:00Z tokens_out rollup=99 events=100; event_count rollup=3 events=2"
        in out
    )
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
    assert code == 0 and out.endswith("reconcile: hours=1 rows=1 drift=0\n")


def test_hour_that_is_not_closed_is_a_bad_argument(closed, capsys):
    assert reconcile_run(capsys, "--hour", "2026-11-10T13:00:00Z")[0] == 2


@pytest.mark.parametrize(
    "argv",
    [
        ["--hour", "2026-11-10T10:30:00Z"],
        ["--hour", "2026-11-10T10:00:00"],
        ["--since", "nope"],
        ["--tolerance-bps", "-1"],
        ["--tolerance-bps", "1.5"],
        ["--hour", HOUR, "--since", HOUR],
    ],
)
def test_bad_arguments_exit_2(closed, capsys, argv):
    assert reconcile_run(capsys, *argv)[0] == 2


def test_actuals_within_tolerance_pass_and_the_exact_boundary_passes(closed, tmp_path, capsys):
    # 100 bps of 200 is 2 tokens; 100 bps of 100 is 1 token. Rollup is 200 in, 100 out.
    path = actuals_file(tmp_path, actual(tokens_in=202, tokens_out=101), actual(tenant="t2", tokens_in=7, tokens_out=3))
    code, out, _ = reconcile_run(capsys, "--actuals", path)
    assert code == 0 and out.endswith("reconcile: hours=1 rows=4 drift=0\n")


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
    path = actuals_file(
        tmp_path, actual(), actual(tenant="t2", tokens_in=7, tokens_out=3), actual(hour="2026-11-10T15:00:00Z")
    )
    code, _, err = reconcile_run(capsys, "--actuals", path)
    assert code == 0
    assert "skipped 1 actuals rows" in err


@pytest.mark.parametrize(
    "bad",
    [
        '{"tenant_id": "t1"',
        "[1]",
        json.dumps({**actual(), "extra": 1}),
        json.dumps(actual(tokens_in=1.5)),
        json.dumps(actual(tokens_in=-1)),
        json.dumps(actual(tokens_in=True)),
        json.dumps(actual(hour="2026-11-10T10:30:00Z")),
        json.dumps(actual(hour="2026-11-10T10:00:00")),
    ],
)
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
    before = closed.execute(
        "SELECT (SELECT count(*) FROM usage_events) AS e, (SELECT count(*) FROM usage_rollups_hourly) AS r,"
        " (SELECT count(*) FROM closed_hours) AS c"
    ).fetchone()
    reconcile_run(capsys)
    closed.rollback()
    assert (
        closed.execute(
            "SELECT (SELECT count(*) FROM usage_events) AS e, (SELECT count(*) FROM usage_rollups_hourly) AS r,"
            " (SELECT count(*) FROM closed_hours) AS c"
        ).fetchone()
        == before
    )


# ---- gaps: a rollup the bill uses but closed_hours does not list, and usage that is never billed ----

NOW = datetime.fromisoformat("2026-11-10T15:00:00+00:00")
H12 = datetime.fromisoformat("2026-11-10T12:00:00+00:00")  # an hour that ended at 13:00


def unbilled(conn, now=NOW, since=None, only=None):
    """The not-billed part of a run with a fixed clock: {'late'|'unclosed'|'future': (events, tokens_in, tokens_out)}."""
    found = reconcile.run(conn, since, only, None, 100, now=now)[4]
    return {kind: (n["events"], n["tokens_in"], n["tokens_out"]) for kind, n in found.items()}


def test_rollup_rows_whose_hour_is_not_in_closed_hours_are_drift(closed, capsys):
    closed.execute("UPDATE usage_rollups_hourly SET tokens_in = tokens_in * 100")  # the inflation the bill would use
    closed.execute("DELETE FROM closed_hours")
    closed.commit()
    code, out, _ = reconcile_run(capsys)
    assert code == 1
    assert (
        "DRIFT tenant=t1 model=mA hour=2026-11-10T10:00:00Z rollup row but its hour is not in closed_hours (rollup=(20000, 100, 2))"
        in out
    )
    assert "DRIFT tenant=t2 model=mA hour=2026-11-10T10:00:00Z rollup row but its hour is not in closed_hours" in out
    assert out.endswith("hours=0 rows=2 drift=2\n")


def test_a_rollup_row_for_an_hour_that_never_closed_is_drift(closed, capsys):
    closed.execute("INSERT INTO usage_rollups_hourly VALUES ('t1', 'mA', %s, 1, 1, 1)", (H12,))
    closed.commit()
    code, out, _ = reconcile_run(capsys)
    assert code == 1 and "hour=2026-11-10T12:00:00Z rollup row but its hour is not in closed_hours" in out
    assert out.endswith("hours=1 rows=3 drift=1\n")


def test_hour_and_since_limit_the_orphan_check(closed, capsys):
    closed.execute("INSERT INTO usage_rollups_hourly VALUES ('t1', 'mA', %s, 1, 1, 1)", (H12,))
    closed.commit()
    assert reconcile_run(capsys, "--hour", HOUR)[0] == 0  # the orphan is in another hour
    assert reconcile_run(capsys, "--since", "2026-11-10T12:00:00Z")[0] == 1  # ... and inside this window
    assert reconcile_run(capsys, "--since", "2026-11-10T13:00:00Z")[0] == 0  # ... and outside this one


def test_the_not_billed_section_is_always_printed_and_zero_when_clean(closed, capsys):
    code, out, _ = reconcile_run(capsys)
    assert code == 0
    lines = out.splitlines()
    assert lines[-4:-1] == [
        "not billed: late events=0 tokens_in=0 tokens_out=0 (its hour was already closed when it arrived)",
        "not billed: unclosed events=0 tokens_in=0 tokens_out=0 (its hour ended over 2 hours ago and is still not closed)",
        "not billed: future events=0 tokens_in=0 tokens_out=0 (dated over 1 hour ahead)",
    ]
    assert lines[-1] == "reconcile: hours=1 rows=2 drift=0"


def test_late_events_are_counted_and_summed_but_do_not_change_the_exit_code(closed, capsys):
    add_event(closed, "l1", tokens_in=5000, tokens_out=7, late=True)
    add_event(closed, "l2", tenant="t2", tokens_in=1, tokens_out=2, late=True)
    code, out, _ = reconcile_run(capsys)
    assert code == 0
    assert "not billed: late events=2 tokens_in=5001 tokens_out=9 (" in out
    assert "not billed: unclosed events=0" in out and "not billed: future events=0" in out


def test_late_sums_past_bigint_are_exact(closed, capsys):
    big = 2**63 - 1
    add_event(closed, "l1", tokens_in=big, tokens_out=1, late=True)
    add_event(closed, "l2", tokens_in=big, tokens_out=1, late=True)
    code, out, _ = reconcile_run(capsys)
    assert code == 0 and f"not billed: late events=2 tokens_in={2 * big} tokens_out=2 (" in out


def test_a_late_event_is_counted_once_even_if_its_hour_is_far_ahead_or_old(closed):
    add_event(closed, "l1", when="2026-11-10T10:30:00Z", late=True)
    add_event(closed, "l2", when="9999-12-31T23:30:00Z", late=True)  # flagged late: counted as late, not also as future
    nb = unbilled(closed)
    assert nb["late"][0] == 2 and nb["future"][0] == 0 and nb["unclosed"][0] == 0


def test_an_hour_still_open_more_than_two_hours_after_it_ended_is_reported(conn):
    add_event(conn, "a", when="2026-11-10T12:30:00Z", tokens_in=11, tokens_out=3)
    assert unbilled(conn, now=datetime.fromisoformat("2026-11-10T15:00:00+00:00"))["unclosed"] == (
        0,
        0,
        0,
    )  # ended exactly 2 h ago
    assert unbilled(conn, now=datetime.fromisoformat("2026-11-10T15:00:00.000001+00:00"))["unclosed"] == (
        1,
        11,
        3,
    )  # a moment over


def test_closing_the_hour_removes_it_from_unclosed(conn):
    add_event(conn, "a", when="2026-11-10T12:30:00Z")
    close_hour(conn, H12)
    assert unbilled(conn)["unclosed"] == (0, 0, 0)


def test_events_dated_far_ahead_are_reported_including_the_last_year(conn):
    add_event(conn, "near", when="2026-11-10T16:00:00Z")  # exactly 1 h ahead of NOW: ordinary clock skew
    assert unbilled(conn)["future"] == (0, 0, 0)
    add_event(conn, "far", when="2026-11-10T16:00:01Z", tokens_in=4, tokens_out=5)
    add_event(conn, "last", when="9999-12-31T23:30:00Z", tokens_in=6, tokens_out=7)
    assert unbilled(conn)["future"] == (2, 10, 12)
    assert unbilled(conn)["unclosed"] == (0, 0, 0)  # not also counted as an overdue hour


def test_hour_and_since_limit_the_not_billed_numbers(closed):
    add_event(closed, "l1", when="2026-11-10T10:20:00Z", late=True)
    add_event(closed, "l2", when="2026-11-10T12:20:00Z", late=True)
    assert unbilled(closed)["late"][0] == 2
    assert unbilled(closed, only=H)["late"][0] == 1
    assert unbilled(closed, since=H12)["late"][0] == 1


def test_fail_on_unbilled_turns_the_not_billed_numbers_into_exit_1(closed, capsys):
    assert reconcile_run(capsys, "--fail-on-unbilled")[0] == 0  # nothing unbilled
    add_event(closed, "l1", late=True)
    assert reconcile_run(capsys)[0] == 0  # default: information only
    assert reconcile_run(capsys, "--fail-on-unbilled")[0] == 1


def test_json_output_is_one_object_with_the_same_numbers_and_exit_code(closed, capsys):
    add_event(closed, "l1", tokens_in=5, tokens_out=6, late=True)
    closed.execute("UPDATE usage_rollups_hourly SET tokens_in = 1 WHERE tenant_id = 't2'")
    closed.commit()
    code, out, _ = reconcile_run(capsys, "--json")
    report = json.loads(out)  # the whole stdout is one JSON document
    assert code == 1
    assert report["hours"] == 1 and report["rows"] == 2
    assert len(report["drift"]) == 1 and report["drift"][0].startswith("DRIFT tenant=t2 ")
    assert report["not_billed"]["late"] == {"events": 1, "tokens_in": 5, "tokens_out": 6}
    assert set(report["not_billed"]) == {"late", "unclosed", "future"}


def test_a_name_with_a_newline_or_an_escape_cannot_forge_a_report_line():
    # tenant and model names come from callers (up to 200 characters, any character): a name must stay on its own line.
    hour = datetime(2026, 11, 1, 10)
    hostile = "m\nreconcile: hours=9 rows=9 drift=0\x1b[32m ALL CLEAN"
    line = reconcile.label("t\r1", hostile, hour)
    assert "\n" not in line and "\r" not in line and "\x1b" not in line
    assert line.startswith("tenant='t\\r1' model='m\\nreconcile: hours=9")


def test_an_ordinary_name_is_printed_as_it_is():
    assert (
        reconcile.label("alice", "gpt-4o", datetime(2026, 11, 1, 10))
        == "tenant=alice model=gpt-4o hour=2026-11-01T10:00:00Z"
    )
