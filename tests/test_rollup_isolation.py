"""One poisoned hour must not block the hours after it, and must be visible."""
import logging
from datetime import datetime, timezone

import psycopg
import pytest

from app import db, rollup
from app.rollup import close_due_hours, close_hour
from tests.test_rollup import add_event, at, closed_hours, rollups

POISON = datetime(2026, 11, 10, 8, tzinfo=timezone.utc)
NOW = at("2026-11-10T12:00:00Z")


def poison_hour_8(monkeypatch):
    real = close_hour

    def flaky(conn, hour):
        if hour == POISON:
            conn.execute("SELECT 1")  # leave a transaction open, as a real failure would
            raise RuntimeError("boom")
        return real(conn, hour)

    monkeypatch.setattr(rollup, "close_hour", flaky)


def three_hours(conn):
    add_event(conn, "h8", "2026-11-10T08:30:00Z", tokens_in=8)
    add_event(conn, "h9", "2026-11-10T09:30:00Z", tokens_in=9)
    add_event(conn, "h10", "2026-11-10T10:30:00Z", tokens_in=10)


def test_a_failed_hour_does_not_block_later_hours_and_is_logged_with_its_hour(conn, monkeypatch, caplog):
    three_hours(conn)
    poison_hour_8(monkeypatch)
    failed = []
    with caplog.at_level(logging.ERROR, logger="app.rollup"):
        closed = close_due_hours(conn, NOW, failed=failed)
    assert [h.hour for h, _, _ in closed] == [9, 10]
    assert failed == [POISON]
    assert [h.hour for h in closed_hours(conn)] == [9, 10]
    assert "rollup failed: hour=2026-11-10T08:00:00+00:00 error=RuntimeError('boom')" in caplog.text
    assert "t1" not in caplog.text  # tenant-free


def test_a_real_overflow_is_isolated_and_leaves_nothing_half_written(conn, caplog):
    add_event(conn, "big1", "2026-11-10T08:10:00Z", tokens_in=5 * 10**18)
    add_event(conn, "big2", "2026-11-10T08:20:00Z", tokens_in=5 * 10**18)
    add_event(conn, "ok", "2026-11-10T09:30:00Z", tokens_in=9)
    failed = []
    closed = close_due_hours(conn, NOW, failed=failed)
    assert [h.hour for h, _, _ in closed] == [9]
    assert failed == [POISON]
    assert closed_hours(conn) == [datetime(2026, 11, 10, 9, tzinfo=timezone.utc)]
    assert [r["hour_start"].hour for r in rollups(conn)] == [9]  # no rollup row, no closed_hours row for hour 8
    assert "hour=2026-11-10T08:00:00+00:00" in caplog.text


def test_the_failed_hour_is_retried_and_closes_once_the_cause_is_gone(conn, monkeypatch):
    three_hours(conn)
    with monkeypatch.context() as m:
        poison_hour_8(m)
        close_due_hours(conn, NOW)
    assert POISON not in closed_hours(conn)
    closed = close_due_hours(conn, NOW)
    assert [h.hour for h, _, _ in closed] == [8]
    assert [h.hour for h in closed_hours(conn)] == [8, 9, 10]


def test_closing_hours_out_of_order_does_not_make_a_later_event_late(conn, monkeypatch):
    from fastapi.testclient import TestClient
    from app.edge import require_entitlement
    from app.main import app

    three_hours(conn)
    poison_hour_8(monkeypatch)
    close_due_hours(conn, NOW)  # hours 9 and 10 closed, 8 still open
    app.dependency_overrides[require_entitlement] = lambda: "t1"
    try:
        resp = TestClient(app).post("/v1/usage", json={
            "tenant_id": "t1", "idempotency_key": "new", "model": "mA", "tokens_in": 1, "tokens_out": 1,
            "occurred_at": "2026-11-10T08:45:00Z"})
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 201
    assert resp.json()["late"] is False  # hour 8 is not closed, whatever the later hours did


def test_one_shot_run_exits_non_zero_and_names_the_failed_hour(conn, monkeypatch, capsys):
    three_hours(conn)
    poison_hour_8(monkeypatch)
    monkeypatch.setattr(rollup, "datetime", type("D", (datetime,), {"now": classmethod(lambda cls, tz=None: NOW)}))
    with pytest.raises(SystemExit) as exit_info:
        rollup.main([])
    assert exit_info.value.code == 1
    out = capsys.readouterr().out
    assert "closed hour=2026-11-10T09:00:00+00:00" in out
    assert "rollup: closed 2 hours, failed 1: 2026-11-10T08:00:00+00:00" in out


def test_every_keeps_looping_after_a_failed_hour(conn, monkeypatch, capsys):
    class StopLoop(Exception):
        pass

    three_hours(conn)
    poison_hour_8(monkeypatch)
    monkeypatch.setattr(rollup, "datetime", type("D", (datetime,), {"now": classmethod(lambda cls, tz=None: NOW)}))
    sleeps = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 2:
            raise StopLoop

    monkeypatch.setattr(rollup.time, "sleep", fake_sleep)
    with pytest.raises(StopLoop):
        rollup.main(["--every", "60"])
    assert sleeps == [60, 60]
    assert capsys.readouterr().out.count("failed 1: 2026-11-10T08:00:00+00:00") == 2  # one failure line per completed cycle


def kill_connection_of_hour_8(monkeypatch, victim_hour=POISON):
    """Close for `victim_hour` (default 8) loses its connection (terminated from the server side) mid-close."""
    real = close_hour

    def killed(conn, hour):
        if hour == victim_hour:
            conn.execute("SELECT 1")
            with db.connect() as killer:
                killer.execute("SELECT pg_terminate_backend(%s)", (conn.info.backend_pid,))
        return real(conn, hour)

    monkeypatch.setattr(rollup, "close_hour", killed)


def test_a_lost_connection_fails_one_hour_and_the_hours_after_it_still_close(conn, monkeypatch, caplog):
    three_hours(conn)
    kill_connection_of_hour_8(monkeypatch)
    opened = []

    def reconnect():
        opened.append(db.connect(statement_timeout_ms=0))
        return opened[-1]

    failed = []
    victim = db.connect(statement_timeout_ms=0)
    try:
        with caplog.at_level(logging.ERROR, logger="app.rollup"):
            closed = close_due_hours(victim, NOW, failed=failed, reconnect=reconnect)
    finally:
        victim.close()
    assert failed == [POISON]
    assert [h.hour for h, _, _ in closed] == [9, 10]
    assert [(r["hour_start"].hour, r["tokens_in"]) for r in rollups(conn)] == [(9, 9), (10, 10)]
    assert [h.hour for h in closed_hours(conn)] == [9, 10]
    assert caplog.text.count("rollup failed: hour=") == 1
    assert "hour=2026-11-10T08:00:00+00:00" in caplog.text
    assert len(opened) == 1 and opened[0].closed  # the connection close_due_hours asked for is closed again


def test_without_reconnect_a_lost_connection_fails_the_rest_as_before(conn, monkeypatch):
    three_hours(conn)
    kill_connection_of_hour_8(monkeypatch)
    failed = []
    victim = db.connect(statement_timeout_ms=0)
    try:
        assert close_due_hours(victim, NOW, failed=failed) == []
    finally:
        victim.close()
    assert [h.hour for h in failed] == [8, 9, 10]


def test_run_once_closes_the_hours_after_one_that_lost_its_connection(conn, monkeypatch, capsys):
    three_hours(conn)
    kill_connection_of_hour_8(monkeypatch)
    monkeypatch.setattr(rollup, "datetime", type("D", (), {"now": staticmethod(lambda tz=None: NOW)}))
    assert rollup.run_once() == [POISON]
    assert [h.hour for h in closed_hours(conn)] == [9, 10]
    out = capsys.readouterr().out
    assert "closed hour=2026-11-10T09:00:00+00:00" in out
    assert "rollup: closed 2 hours, failed 1: 2026-11-10T08:00:00+00:00" in out
    monkeypatch.undo()
    assert [h.hour for h, _, _ in close_due_hours(conn, NOW)] == [8]  # the next cycle closes it


def test_a_reconnect_that_fails_fails_that_hour_and_the_next_hour_tries_again(conn, monkeypatch, caplog):
    three_hours(conn)
    add_event(conn, "h7", "2026-11-10T07:30:00Z", tokens_in=7)
    kill_connection_of_hour_8(monkeypatch)
    calls = []

    def reconnect():
        calls.append(1)
        raise psycopg.OperationalError("database still down")

    failed = []
    victim = db.connect(statement_timeout_ms=0)
    try:
        with caplog.at_level(logging.ERROR, logger="app.rollup"):
            closed = close_due_hours(victim, NOW, failed=failed, reconnect=reconnect)
    finally:
        victim.close()
    assert [h.hour for h, _, _ in closed] == [7]  # closed before the loss: still returned
    assert [h.hour for h in failed] == [8, 9, 10]
    assert len(calls) == 2  # hours 9 and 10 each tried to reconnect; hour 8 was the one that lost it
    assert [h.hour for h in closed_hours(conn)] == [7]
    assert "rollup failed: hour=2026-11-10T09:00:00+00:00 error=OperationalError('database still down')" in caplog.text
    assert "rollup failed: hour=2026-11-10T10:00:00+00:00 error=OperationalError('database still down')" in caplog.text
