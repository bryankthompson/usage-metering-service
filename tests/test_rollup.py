import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db, rollup
from app.edge import require_entitlement
from app.ingest import INSERT_EVENT
from app.main import app
from app.rollup import HOUR_LOCK_CLASS, close_due_hours, close_hour, hour_index, lock_hour_shared

ROOT = Path(__file__).resolve().parent.parent
H = datetime(2026, 11, 10, 10, tzinfo=timezone.utc)  # the hour most tests close


def at(text):
    return datetime.fromisoformat(text)


def add_event(conn, key, when, tenant="t1", model="mA", tokens_in=1, tokens_out=1, late=False):
    conn.execute(
        "INSERT INTO usage_events (tenant_id, idempotency_key, model, tokens_in, tokens_out, occurred_at, late)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (tenant, key, model, tokens_in, tokens_out, at(when), late))
    conn.commit()


def rollups(conn):
    rows = conn.execute("SELECT * FROM usage_rollups_hourly ORDER BY tenant_id, model, hour_start").fetchall()
    conn.commit()
    return rows


def closed_hours(conn):
    rows = conn.execute("SELECT hour_start FROM closed_hours ORDER BY hour_start").fetchall()
    conn.commit()
    return [r["hour_start"] for r in rows]


def test_close_hour_sums_per_tenant_and_model_for_that_hour_only(conn):
    add_event(conn, "a", "2026-11-10T10:05:00Z", tokens_in=10, tokens_out=1)
    add_event(conn, "b", "2026-11-10T10:55:00Z", tokens_in=20, tokens_out=2)
    add_event(conn, "c", "2026-11-10T10:10:00Z", model="mB", tokens_in=5, tokens_out=5)
    add_event(conn, "d", "2026-11-10T10:30:00Z", tenant="t2", tokens_in=7, tokens_out=3)
    add_event(conn, "before", "2026-11-10T09:59:59.999999Z", tokens_in=100)
    add_event(conn, "after", "2026-11-10T11:00:00Z", tokens_in=100)
    add_event(conn, "late", "2026-11-10T10:20:00Z", tokens_in=1000, tokens_out=1000, late=True)

    assert close_hour(conn, H) == (3, 4)

    got = {(r["tenant_id"], r["model"]): (r["hour_start"], r["tokens_in"], r["tokens_out"], r["event_count"])
           for r in rollups(conn)}
    assert got == {
        ("t1", "mA"): (H, 30, 3, 2),
        ("t1", "mB"): (H, 5, 5, 1),
        ("t2", "mA"): (H, 7, 3, 1),
    }
    assert closed_hours(conn) == [H]


def test_closing_an_hour_twice_changes_nothing(conn):
    add_event(conn, "a", "2026-11-10T10:05:00Z", tokens_in=10)
    assert close_hour(conn, H) == (1, 1)
    before = rollups(conn)
    add_event(conn, "sneaked-in", "2026-11-10T10:06:00Z", tokens_in=99)  # bypasses ingest, which would flag it late

    assert close_hour(conn, H) is None
    assert rollups(conn) == before
    assert closed_hours(conn) == [H]


def test_an_empty_hour_closes_with_no_rollup_rows(conn):
    assert close_hour(conn, H) == (0, 0)
    assert rollups(conn) == []
    assert closed_hours(conn) == [H]


def test_close_hour_takes_any_offset_that_is_a_utc_hour(conn):
    assert close_hour(conn, at("2026-11-10T15:30:00+05:30")) == (0, 0)
    assert closed_hours(conn) == [H]


@pytest.mark.parametrize("hour_start", [
    datetime(2026, 11, 10, 10),                              # naive
    datetime(2026, 11, 10, 10, 30, tzinfo=timezone.utc),     # not on an hour boundary
    datetime(2026, 11, 10, 10, 0, 1, tzinfo=timezone.utc),
])
def test_close_hour_rejects_a_bad_hour_and_writes_nothing(conn, hour_start):
    with pytest.raises(ValueError):
        close_hour(conn, hour_start)
    assert closed_hours(conn) == []


@pytest.fixture
def client(conn):
    app.dependency_overrides[require_entitlement] = lambda: "t1"
    yield TestClient(app)
    app.dependency_overrides.clear()


def body(key, when, **changes):
    return {"tenant_id": "t1", "idempotency_key": key, "model": "mA", "tokens_in": 10, "tokens_out": 1,
            "occurred_at": when, **changes}


def test_late_and_duplicate_events_never_change_a_closed_rollup(client, conn):
    assert client.post("/v1/usage", json=body("a", "2026-11-10T10:20:00Z")).status_code == 201
    assert close_hour(conn, H) == (1, 1)
    closed = rollups(conn)
    assert closed[0]["event_count"] == 1

    late = client.post("/v1/usage", json=body("b", "2026-11-10T10:40:00Z"))
    resend = client.post("/v1/usage", json=body("a", "2026-11-10T10:20:00Z"))
    conflict = client.post("/v1/usage", json=body("a", "2026-11-10T10:20:00Z", tokens_in=11))
    assert (late.status_code, resend.status_code, conflict.status_code) == (201, 200, 409)

    assert rollups(conn) == closed
    stored = {r["idempotency_key"]: r["late"] for r in conn.execute("SELECT idempotency_key, late FROM usage_events")}
    assert stored == {"a": False, "b": True}  # the late event is in the log, flagged, and not in the rollup
    assert close_hour(conn, H) is None
    assert rollups(conn) == closed


def wait_until_blocked(watcher):
    """Wait until some session is waiting for an advisory lock."""
    for _ in range(500):
        waiting = watcher.execute(
            "SELECT count(*) AS n FROM pg_locks WHERE locktype = 'advisory' AND NOT granted").fetchone()["n"]
        watcher.commit()
        if waiting:
            return
        time.sleep(0.01)
    pytest.fail("nothing is waiting for the hour lock")


def test_an_event_that_arrives_while_the_hour_is_closing_is_late(client, conn):
    closer = db.connect()
    try:
        # A close in progress: it holds the hour's exclusive lock and has not committed yet.
        closer.execute("SELECT pg_advisory_xact_lock(%s, %s)", (HOUR_LOCK_CLASS, hour_index(H)))
        with ThreadPoolExecutor(max_workers=1) as pool:
            try:
                posted = pool.submit(client.post, "/v1/usage", json=body("arrives-mid-close", "2026-11-10T10:30:00Z"))
                wait_until_blocked(conn)
                assert not posted.done()  # the ingest waits for the close instead of racing it
                assert close_hour(closer, H) == (0, 0)  # the close finishes without that event, then commits
                response = posted.result(timeout=10)
            finally:
                closer.rollback()  # release the lock so the worker can end even when an assert failed
    finally:
        closer.close()

    assert response.status_code == 201
    assert conn.execute("SELECT late FROM usage_events").fetchone()["late"] is True
    assert rollups(conn) == []


def test_a_close_waits_for_an_ingest_already_in_flight_and_counts_it(conn):
    ingester = db.connect()
    try:
        # An ingest part way through: it holds the shared lock and has inserted, but has not committed.
        when = H + timedelta(minutes=30)
        lock_hour_shared(ingester, when)
        row = ingester.execute(INSERT_EVENT, {
            "tenant_id": "t1", "idempotency_key": "in-flight", "model": "mA", "tokens_in": 10, "tokens_out": 1,
            "occurred_at": when}).fetchone()
        assert row is not None

        def close_on_own_connection():
            closer = db.connect()
            try:
                return close_hour(closer, H)
            finally:
                closer.close()

        with ThreadPoolExecutor(max_workers=1) as pool:
            try:
                closing = pool.submit(close_on_own_connection)
                wait_until_blocked(conn)
                assert not closing.done()  # the close waits for the ingest
                ingester.commit()
                assert closing.result(timeout=10) == (1, 1)
            finally:
                ingester.rollback()  # no-op after the commit; frees the lock if an assert failed first
    finally:
        ingester.close()

    assert conn.execute("SELECT late FROM usage_events").fetchone()["late"] is False
    assert [r["event_count"] for r in rollups(conn)] == [1]


def test_close_due_hours_honours_the_grace_period(conn):
    add_event(conn, "h8", "2026-11-10T08:30:00Z")
    add_event(conn, "h9", "2026-11-10T09:30:00Z")
    add_event(conn, "h10", "2026-11-10T10:30:00Z")

    def hour(h):
        return datetime(2026, 11, 10, h, tzinfo=timezone.utc)

    assert close_due_hours(conn, at("2026-11-10T09:04:59Z")) == []   # hour 8 ended 4m59s ago
    assert close_due_hours(conn, at("2026-11-10T09:04:59Z"), grace=timedelta(0)) == [(hour(8), 1, 1)]
    assert close_due_hours(conn, at("2026-11-10T10:04:59Z")) == []   # hour 9 ended 4m59s ago
    assert close_due_hours(conn, at("2026-11-10T10:05:00Z")) == [(hour(9), 1, 1)]  # exactly the grace
    assert close_due_hours(conn, at("2026-11-10T10:05:00Z")) == []   # nothing new is due
    assert close_due_hours(conn, at("2026-11-10T11:05:00Z")) == [(hour(10), 1, 1)]
    assert closed_hours(conn) == [hour(8), hour(9), hour(10)]


def test_close_due_hours_closes_oldest_first(conn):
    add_event(conn, "h10", "2026-11-10T10:30:00Z")
    add_event(conn, "h8", "2026-11-10T08:30:00Z")
    add_event(conn, "h9", "2026-11-10T09:30:00Z")
    closed = close_due_hours(conn, at("2026-11-10T11:05:00Z"))
    assert [h.hour for h, _, _ in closed] == [8, 9, 10]
    assert [h.hour for h in closed_hours(conn)] == [8, 9, 10]  # one closed_hours row each


def test_close_due_hours_needs_a_timezone_aware_now(conn):
    with pytest.raises(ValueError):
        close_due_hours(conn, datetime(2026, 11, 10, 12))


def run_cli(*args, database_url=None):
    env = None
    if database_url:
        env = {**os.environ, "DATABASE_URL": database_url}
    return subprocess.run([sys.executable, "-m", "app.rollup", *args], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=60)


def test_cli_closes_due_hours_once_and_says_when_nothing_is_due(conn):
    add_event(conn, "old-1", "2026-01-01T10:05:00Z")
    add_event(conn, "old-2", "2026-01-01T10:15:00Z", model="mB")

    first = run_cli()
    assert first.returncode == 0, first.stderr
    assert first.stdout.strip() == "closed hour=2026-01-01T10:00:00+00:00 rows=2 events=2"

    second = run_cli()
    assert second.returncode == 0, second.stderr
    assert second.stdout.strip() == "nothing due"


def test_cli_fails_loudly_when_the_database_is_unreachable():
    result = run_cli(database_url="postgresql://metering:metering@localhost:1/metering")
    assert result.returncode != 0
    assert "OperationalError" in result.stderr


def test_every_runs_again_after_each_sleep(conn, monkeypatch, capsys):
    class StopLoop(Exception):
        pass

    sleeps = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 2:
            raise StopLoop

    monkeypatch.setattr(rollup.time, "sleep", fake_sleep)
    with pytest.raises(StopLoop):
        rollup.main(["--every", "3600"])
    assert sleeps == [3600, 3600]
    assert capsys.readouterr().out.count("nothing due") == 2  # one run at start, one after the first sleep


@pytest.mark.parametrize("every", ["0", "-1"])
def test_every_must_be_positive(every, capsys):
    with pytest.raises(SystemExit) as exit_info:
        rollup.main(["--every", every])
    assert exit_info.value.code == 2
    assert "positive" in capsys.readouterr().err
