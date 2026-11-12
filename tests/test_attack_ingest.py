"""Red-team tests for usage intake and the rollup: hostile bodies, extreme timestamps, races, a dying database.

Each test guards something that failed (or could fail silently) when it was first tried; the docstring says what.
"""

import json
import logging
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import psycopg
import pytest
from fastapi.testclient import TestClient

from app import db, rollup
from app.edge import require_entitlement
from app.main import app
from app.rollup import HOUR_LOCK_CLASS, close_due_hours, close_hour, hour_index

H = datetime(2026, 11, 10, 10, tzinfo=timezone.utc)
FAR_FUTURE = datetime(2030, 1, 1, tzinfo=timezone.utc)  # a "now" after every hour these tests use


def event(**changes):
    base = {
        "tenant_id": "t1",
        "idempotency_key": "k1",
        "model": "m1",
        "tokens_in": 10,
        "tokens_out": 5,
        "occurred_at": "2026-11-10T10:30:00Z",
    }
    return {**base, **changes}


@pytest.fixture
def client(conn):
    """A client whose edge admits t1. A 500 comes back as a 500, not as a raised exception."""
    app.dependency_overrides[require_entitlement] = lambda: "t1"
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()


def count(conn, table="usage_events"):
    n = conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"]
    conn.commit()
    return n


def blocked(conn, locktype):
    """Sessions in this database waiting for a lock of this type (pg_locks is cluster-wide: scripts/test shares one Postgres)."""
    n = conn.execute(
        "SELECT count(*) AS n FROM pg_locks WHERE locktype = %s AND NOT granted"
        " AND database = (SELECT oid FROM pg_database WHERE datname = current_database())",
        (locktype,),
    ).fetchone()["n"]
    conn.commit()
    return n


def wait_until_blocked(conn, locktype="advisory"):
    for _ in range(500):
        if blocked(conn, locktype):
            return
        time.sleep(0.01)
    pytest.fail(f"nothing is waiting for a {locktype} lock")


# --- hostile bodies: every one is a 422 (it was a 500, or stored a row that later broke the rollup) ----------------


def raw(**fields):
    return json.dumps(event(**fields))


SURROGATE = '{"tenant_id":"t1","idempotency_key":"k","model":"\\ud800","tokens_in":1,"tokens_out":1,"occurred_at":"2026-11-10T10:30:00Z"}'
NAN = '{"tenant_id":"t1","idempotency_key":"k","model":"m","tokens_in":NaN,"tokens_out":1,"occurred_at":"2026-11-10T10:30:00Z"}'
INFINITY = NAN.replace("NaN", "-Infinity")


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(raw(model="a\u0000b"), id="NUL in model"),
        pytest.param(raw(idempotency_key="a\u0000b"), id="NUL in key"),
        pytest.param(SURROGATE, id="lone surrogate"),
        pytest.param(NAN, id="NaN count"),
        pytest.param(INFINITY, id="Infinity count"),
        pytest.param(raw(model="m" * 201), id="model over 200 chars"),
        pytest.param(raw(tenant_id="t" * 201), id="tenant over 200 chars"),
        pytest.param(raw(occurred_at="0001-01-01T00:00:00+05:00"), id="UTC instant before year 1"),
        pytest.param(raw(occurred_at="9999-12-31T23:59:59.999999-05:00"), id="UTC instant after year 9999"),
        pytest.param(raw(occurred_at=1762770600), id="unix seconds as a number"),
        pytest.param(raw(occurred_at=5), id="small number as a timestamp"),
        pytest.param(raw(occurred_at="1762770600"), id="unix seconds as a string"),
        pytest.param(raw(occurred_at="-1"), id="negative number as a string"),
        pytest.param(raw(occurred_at=True), id="bool as a timestamp"),
        pytest.param(raw(tokens_in=5.0), id="float count"),
        pytest.param(raw(tokens_in=True), id="bool count"),
        pytest.param(raw(tokens_in="5"), id="string count"),
        pytest.param(raw(model=None), id="null model"),
        pytest.param("[]", id="array body"),
        pytest.param("{", id="broken JSON"),
    ],
)
def test_a_hostile_body_is_a_422_that_stores_nothing(client, conn, body):
    r = client.post("/v1/usage", content=body, headers={"content-type": "application/json"})
    assert r.status_code == 422, r.text
    assert "input" not in r.text  # the 422 does not echo the input (it is what turned NaN into a 500)
    assert count(conn) == 0


def test_the_longest_accepted_values_are_stored_resent_and_rolled_up(conn):
    """200 four-byte characters in each text field is the worst case for the btree key limit."""
    tenant = model = "\U0001f600" * 200
    app.dependency_overrides[require_entitlement] = lambda: tenant
    try:
        client = TestClient(app, raise_server_exceptions=False)
        body = event(tenant_id=tenant, model=model, idempotency_key="\U0001f600" * 200)
        assert client.post("/v1/usage", json=body).status_code == 201
        assert client.post("/v1/usage", json=body).status_code == 200
    finally:
        app.dependency_overrides.clear()
    assert close_hour(conn, H) == (1, 1)


@pytest.mark.parametrize(
    "when, hour",
    [
        ("0001-01-01T00:00:00Z", datetime(1, 1, 1, tzinfo=timezone.utc)),
        ("9999-12-31T23:59:59.999999Z", datetime(9999, 12, 31, 23, tzinfo=timezone.utc)),
    ],
)
def test_the_extreme_valid_timestamps_are_stored_and_resent(client, conn, when, hour):
    assert client.post("/v1/usage", json=event(occurred_at=when)).status_code == 201
    assert client.post("/v1/usage", json=event(occurred_at=when)).status_code == 200  # read back without an error
    assert close_hour(conn, hour) == (1, 1)


def test_the_idempotency_key_cannot_forge_a_log_line(client, caplog):
    key = "x\nINFO:     usage accepted: tenant=victim key='fake' event_id=999 late=False"
    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        assert client.post("/v1/usage", json=event(idempotency_key=key)).status_code == 201
        assert client.post("/v1/usage", json=event(idempotency_key=key, tokens_in=11)).status_code == 409
        assert client.post("/v1/usage", json=event(idempotency_key=key)).status_code == 200
    lines = [line for line in caplog.text.splitlines() if "usage " in line]
    assert len(lines) == 3, caplog.text  # accepted, conflict, duplicate: one line each, the newline stays escaped


@pytest.mark.parametrize("when", ["2026-11-10T10:30Z", "2026-11-10 10:30:00+00:00", "2026-11-10T10:30:00.5+01:00"])
def test_the_iso_8601_spellings_that_have_a_time_zone_are_still_accepted(client, when):
    assert client.post("/v1/usage", json=event(occurred_at=when)).status_code == 201


# --- hour boundaries: the rollup row lands in the UTC hour of the instant, whatever the offset -----------------------


@pytest.mark.parametrize(
    "occurred_at, utc_hour",
    [
        ("2026-11-10T23:59:59.999999Z", "2026-11-10T23:00:00+00:00"),
        ("2026-11-11T00:00:00Z", "2026-11-11T00:00:00+00:00"),
        ("2026-11-10T23:30:00-05:00", "2026-11-11T04:00:00+00:00"),  # crosses the day
        ("2026-11-10T10:15:00+05:30", "2026-11-10T04:00:00+00:00"),  # a half-hour offset: 04:45 UTC
        ("2026-11-10T00:00:00+14:00", "2026-11-09T10:00:00+00:00"),  # the largest offset, previous day
        ("2026-03-08T01:59:59-05:00", "2026-03-08T06:00:00+00:00"),  # US spring forward: last second before the gap
        ("2026-03-08T03:00:00-04:00", "2026-03-08T07:00:00+00:00"),  # ... and the first after it
        ("2026-11-01T01:30:00-04:00", "2026-11-01T05:00:00+00:00"),  # US fall back: 01:30 happens twice,
        ("2026-11-01T01:30:00-05:00", "2026-11-01T06:00:00+00:00"),  # one hour apart
    ],
)
def test_the_rollup_hour_is_the_utc_hour_of_the_instant(client, conn, occurred_at, utc_hour):
    assert client.post("/v1/usage", json=event(occurred_at=occurred_at)).status_code == 201
    closed = close_due_hours(conn, FAR_FUTURE)
    assert [(h.isoformat(), rows, events) for h, rows, events in closed] == [(utc_hour, 1, 1)]
    row = conn.execute("SELECT hour_start, tokens_in FROM usage_rollups_hourly").fetchone()
    assert (row["hour_start"].isoformat(), row["tokens_in"]) == (utc_hour, 10)


# --- concurrency ----------------------------------------------------------------------------------------------------


def send_together(bodies):
    """POST every body at the same moment, one client per thread. Returns the status codes in input order."""
    start = threading.Barrier(len(bodies))

    def send(body):
        start.wait()
        return TestClient(app, raise_server_exceptions=False).post("/v1/usage", json=body).status_code

    with ThreadPoolExecutor(max_workers=len(bodies)) as pool:
        return [f.result() for f in [pool.submit(send, b) for b in bodies]]


def test_twenty_identical_requests_store_one_event(client, conn):
    codes = send_together([event()] * 20)
    assert sorted(codes) == [200] * 19 + [201]
    assert count(conn) == 1


def test_twenty_conflicting_requests_store_one_event_and_agree_on_the_winner(client, conn):
    bodies = [event(tokens_in=10 + i % 2) for i in range(20)]  # half say 10, half say 11, same key
    codes = send_together(bodies)
    assert codes.count(201) == 1
    winner = bodies[codes.index(201)]
    assert [c for c, b in zip(codes, bodies) if c != 201] == [
        200 if b == winner else 409 for c, b in zip(codes, bodies) if c != 201
    ]
    assert conn.execute("SELECT tokens_in FROM usage_events").fetchone()["tokens_in"] == winner["tokens_in"]
    assert count(conn) == 1


def test_ingest_racing_the_close_is_never_both_and_never_neither(conn):
    """Each event is in the closed rollup or flagged late. Without the hour lock an insert that commits after the
    close read the table but before it was visible as closed is neither (counted nowhere, not late)."""
    app.dependency_overrides[require_entitlement] = lambda: "t1"
    rounds, senders, per_sender = 12, 6, 5
    rng = random.Random(7)
    try:
        for r in range(rounds):
            hour = H + timedelta(hours=r)
            start = threading.Barrier(senders + 1)
            delay = rng.uniform(0, 0.03)

            def send(s):
                client = TestClient(app, raise_server_exceptions=False)
                start.wait(timeout=30)
                return [
                    client.post(
                        "/v1/usage",
                        json=event(
                            idempotency_key=f"r{r}-s{s}-{i}", occurred_at=(hour + timedelta(minutes=10 + i)).isoformat()
                        ),
                    ).status_code
                    for i in range(per_sender)
                ]

            def close():
                start.wait(timeout=30)
                closer = db.connect()
                try:
                    time.sleep(delay)
                    return close_hour(closer, hour)
                finally:
                    closer.close()

            with ThreadPoolExecutor(max_workers=senders + 1) as pool:
                sends = [pool.submit(send, s) for s in range(senders)]
                closing = pool.submit(close)
                statuses = [code for f in sends for code in f.result()]
                closing.result()
            assert statuses == [201] * (senders * per_sender)

            late = conn.execute(
                "SELECT count(*) AS n FROM usage_events WHERE occurred_at >= %s AND late", (hour,)
            ).fetchone()["n"]
            total = conn.execute("SELECT count(*) AS n FROM usage_events WHERE occurred_at >= %s", (hour,)).fetchone()[
                "n"
            ]
            counted = conn.execute(
                "SELECT coalesce(sum(event_count), 0) AS n FROM usage_rollups_hourly WHERE hour_start = %s", (hour,)
            ).fetchone()["n"]
            conn.commit()
            assert (
                counted + late == total == senders * per_sender
            ), f"round {r}: counted={counted} late={late} total={total}"
    finally:
        app.dependency_overrides.clear()


def test_several_rollups_at_once_close_each_hour_once(conn):
    hours = [H + timedelta(hours=i) for i in range(6)]
    for i, hour in enumerate(hours):
        conn.execute(
            "INSERT INTO usage_events (tenant_id, idempotency_key, model, tokens_in, tokens_out, occurred_at)"
            " VALUES ('t1', %s, 'm1', 1, 1, %s)",
            (f"e{i}", hour + timedelta(minutes=5)),
        )
    conn.commit()
    start = threading.Barrier(4)

    def run():
        start.wait(timeout=30)
        mine = db.connect()
        try:
            return close_due_hours(mine, FAR_FUTURE)
        finally:
            mine.close()

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = [f.result() for f in [pool.submit(run) for _ in range(4)]]
    closed = sorted(h for result in results for h, _, _ in result)
    assert closed == hours  # every hour closed by exactly one of the four
    assert count(conn, "usage_rollups_hourly") == 6
    assert count(conn, "closed_hours") == 6


# --- a rollup that dies, and a database that dies --------------------------------------------------------------------


def test_a_close_whose_connection_is_killed_part_way_leaves_the_hour_open(conn):
    """The close has the hour lock and has written closed_hours, then its connection is terminated before the commit."""
    conn.execute(
        "INSERT INTO usage_events (tenant_id, idempotency_key, model, tokens_in, tokens_out, occurred_at)"
        " VALUES ('t1', 'e1', 'm1', 7, 1, %s)",
        (H + timedelta(minutes=5),),
    )
    conn.commit()
    victim = db.connect()
    locker = db.connect()
    try:
        locker.execute("LOCK TABLE usage_events IN ACCESS EXCLUSIVE MODE")  # the aggregate step will wait on this
        with ThreadPoolExecutor(max_workers=1) as pool:
            closing = pool.submit(close_hour, victim, H)
            try:
                wait_until_blocked(conn, "relation")
                conn.execute("SELECT pg_terminate_backend(%s)", (victim.info.backend_pid,))
                conn.commit()
                with pytest.raises(psycopg.OperationalError):
                    closing.result(timeout=10)
            finally:
                locker.rollback()  # release the lock before the pool waits for the worker
    finally:
        locker.close()
        victim.close()
    assert count(conn, "closed_hours") == 0 and count(conn, "usage_rollups_hourly") == 0
    assert close_hour(conn, H) == (1, 1)  # the next run closes it, with the same numbers
    assert conn.execute("SELECT tokens_in FROM usage_rollups_hourly").fetchone()["tokens_in"] == 7


def test_a_close_that_raises_part_way_closes_nothing(conn, monkeypatch):
    conn.execute(
        "INSERT INTO usage_events (tenant_id, idempotency_key, model, tokens_in, tokens_out, occurred_at)"
        " VALUES ('t1', 'e1', 'm1', 1, 1, %s)",
        (H + timedelta(minutes=5),),
    )
    conn.commit()
    monkeypatch.setattr(rollup, "AGGREGATE_HOUR", "SELECT 1/0")  # fails after closed_hours was written
    with pytest.raises(psycopg.errors.DivisionByZero):
        close_hour(conn, H)
    monkeypatch.undo()
    assert count(conn, "closed_hours") == 0 and count(conn, "usage_rollups_hourly") == 0
    assert close_hour(conn, H) == (1, 1)


def test_the_database_unreachable_after_the_edge_is_503(client, monkeypatch):
    def refuse(*args, **kwargs):
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr(db, "request_connection", refuse)
    r = client.post("/v1/usage", json=event())
    assert (r.status_code, r.json()) == (503, {"detail": "database unavailable"})


def test_the_database_dying_mid_request_is_503_and_the_same_key_can_be_resent(client, conn):
    holder = db.connect()  # a close in progress: it holds the hour's exclusive lock, so the ingest waits
    try:
        holder.execute("SELECT pg_advisory_xact_lock(%s, %s)", (HOUR_LOCK_CLASS, hour_index(H)))
        with ThreadPoolExecutor(max_workers=1) as pool:
            posting = pool.submit(client.post, "/v1/usage", json=event())
            try:
                wait_until_blocked(conn)
                conn.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted"
                    " AND database = (SELECT oid FROM pg_database WHERE datname = current_database())"
                )
                conn.commit()
                assert posting.result(timeout=10).status_code == 503
            finally:
                holder.rollback()  # release the lock before the pool waits for the worker
    finally:
        holder.rollback()
        holder.close()
    assert count(conn) == 0
    assert client.post("/v1/usage", json=event()).status_code == 201  # nothing half-stored blocks the retry


@pytest.mark.parametrize("every", ["abc", "1.5", ""])
def test_every_that_is_not_a_whole_number_is_refused_before_anything_runs(every, capsys):
    with pytest.raises(SystemExit) as exit_info:
        rollup.main(["--every", every])
    assert exit_info.value.code == 2
    assert "nothing due" not in capsys.readouterr().out


# --- tenant spoofing through the real edge (no dependency override) -----------------------------------------------------


def test_a_tenant_cannot_post_usage_as_another_tenant(conn):
    for tenant in ("tenant-a", "tenant-b"):
        conn.execute("INSERT INTO entitlements (tenant_id, status) VALUES (%s, 'active')", (tenant,))
    conn.commit()
    assert app.dependency_overrides == {}, "an override left by another test would hide the real edge"
    client = TestClient(app, raise_server_exceptions=False)

    def post(header_tenant, body_tenant, key):
        return client.post(
            "/v1/usage", json=event(tenant_id=body_tenant, idempotency_key=key), headers={"X-Tenant-Id": header_tenant}
        ).status_code

    assert post("tenant-a", "tenant-b", "spoof") == 403  # header says a, body says b: nothing stored for either
    assert post("tenant-b", "tenant-a", "spoof") == 403
    assert post("tenant-a", "TENANT-A", "case") == 403  # the body must match the admitted tenant exactly
    assert post("tenant-a", " tenant-a", "space") == 403
    assert post(" tenant-a ", "tenant-a", "padded") == 201  # the edge trims the header, so this is tenant-a itself
    assert post("tenant-a", "tenant-a", "spoof") == 201  # the refused key was never used up
    assert [r["tenant_id"] for r in conn.execute("SELECT tenant_id FROM usage_events ORDER BY id")] == ["tenant-a"] * 2


# --- the documented limit holds: a sum past BIGINT fails loudly and closes nothing ----------------------------------------


def test_a_rollup_sum_past_bigint_fails_and_leaves_the_hour_open(conn):
    """docs/ingest.md "Known limit": rows are inserted directly because the API caps one event at 10^12."""
    for key in ("a", "b"):
        conn.execute(
            "INSERT INTO usage_events (tenant_id, idempotency_key, model, tokens_in, tokens_out, occurred_at)"
            " VALUES ('t1', %s, 'm1', 5000000000000000000, 0, %s)",
            (key, H + timedelta(minutes=5)),
        )
    conn.commit()
    failed = []
    assert close_due_hours(conn, FAR_FUTURE, failed=failed) == []  # isolated: logged and reported, not raised
    assert failed == [H]
    assert count(conn, "closed_hours") == 0 and count(conn, "usage_rollups_hourly") == 0
