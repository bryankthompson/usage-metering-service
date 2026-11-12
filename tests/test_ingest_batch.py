"""POST /v1/usage/batch: the same dedupe rule as POST /v1/usage, over many events in one transaction, all or nothing.

The single route is the oracle: the batch must store the same rows, with the same `late` flags, and report the same status
per event. The race tests are the money path (an acknowledged event is in the log once, in the right hour, with the right
`late`): see the mutation list in docs/ingest.md.
"""
import json
import logging
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import psycopg
import pytest
from fastapi.testclient import TestClient

from app import db, ingest
from app.edge import require_entitlement
from app.main import app
from app.rollup import HOUR_LOCK_CLASS, close_hour, hour_index
from tests.test_attack_ingest import H, blocked, count, event, wait_until_blocked

BATCH = "/v1/usage/batch"
JSON = {"content-type": "application/json"}


def batch(*events):
    return {"events": list(events)}


def events(n, start=0, **changes):
    return [event(idempotency_key=f"k{i}", **changes) for i in range(start, start + n)]


@pytest.fixture
def client(conn):
    app.dependency_overrides[require_entitlement] = lambda: "t1"
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()


@pytest.fixture
def short_wait(monkeypatch):
    monkeypatch.setattr(db, "LOCK_WAIT", "300ms", raising=False)


def rows(conn):
    got = conn.execute("SELECT idempotency_key, model, tokens_in, tokens_out, occurred_at, late FROM usage_events"
                       " ORDER BY idempotency_key").fetchall()
    conn.commit()
    return got


# --- the route and its answer -------------------------------------------------------------------------------------

def test_a_batch_stores_every_event_and_answers_in_input_order(client, conn, caplog):
    sent = [event(idempotency_key=k, tokens_in=i) for i, k in enumerate(["b", "c", "a"])]
    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        r = client.post(BATCH, json=batch(*sent))
    assert r.status_code == 201
    body = r.json()
    assert (body["accepted"], body["duplicate"]) == (3, 0)
    assert [x["idempotency_key"] for x in body["results"]] == ["b", "c", "a"]
    assert {x["status"] for x in body["results"]} == {"accepted"} and {x["late"] for x in body["results"]} == {False}
    stored = {r["idempotency_key"]: r for r in rows(conn)}
    assert [stored[x["idempotency_key"]]["tokens_in"] for x in body["results"]] == [0, 1, 2]
    ids = conn.execute("SELECT id, idempotency_key FROM usage_events").fetchall()
    assert {i["idempotency_key"]: i["id"] for i in ids} == {x["idempotency_key"]: x["event_id"] for x in body["results"]}
    assert "usage batch: tenant=t1 events=3 accepted=3 duplicate=0" in caplog.text


def test_a_resent_batch_stores_nothing_and_is_200(client, conn):
    first = client.post(BATCH, json=batch(*events(5)))
    again = client.post(BATCH, json=batch(*events(5)))
    assert (first.status_code, again.status_code) == (201, 200)
    assert (again.json()["accepted"], again.json()["duplicate"]) == (0, 5)
    assert [x["event_id"] for x in again.json()["results"]] == [x["event_id"] for x in first.json()["results"]]
    assert count(conn) == 5


def test_a_thousand_events_with_ten_repeats_inside_the_batch_store_990(client, conn):
    sent = events(990) + events(10)                       # the last ten repeat the first ten, identical
    r = client.post(BATCH, json=batch(*sent))
    assert (r.status_code, r.json()["accepted"], r.json()["duplicate"]) == (201, 990, 10)
    statuses = [x["status"] for x in r.json()["results"]]
    assert statuses[:990] == ["accepted"] * 990 and statuses[990:] == ["duplicate"] * 10   # the later one is the duplicate
    assert [x["event_id"] for x in r.json()["results"][990:]] == [x["event_id"] for x in r.json()["results"][:10]]
    assert count(conn) == 990
    again = client.post(BATCH, json=batch(*sent))
    assert (again.status_code, again.json()["accepted"], again.json()["duplicate"]) == (200, 0, 1000)
    assert count(conn) == 990


def test_a_thousand_events_of_which_ten_were_stored_by_single_and_batch_calls_store_990(client, conn):
    assert client.post("/v1/usage", json=event(idempotency_key="k0")).status_code == 201
    for i in range(1, 5):
        assert client.post("/v1/usage", json=event(idempotency_key=f"k{i}")).status_code == 201
    assert client.post(BATCH, json=batch(*events(5, start=5))).status_code == 201
    r = client.post(BATCH, json=batch(*events(1000)))
    assert (r.status_code, r.json()["accepted"], r.json()["duplicate"]) == (201, 990, 10)
    assert count(conn) == 1000


def test_the_same_key_twice_with_the_same_payload_is_one_row_and_a_duplicate(client, conn):
    r = client.post(BATCH, json=batch(event(), event(), event()))
    assert [x["status"] for x in r.json()["results"]] == ["accepted", "duplicate", "duplicate"]
    assert count(conn) == 1


def test_the_same_instant_in_another_offset_is_a_duplicate_in_the_batch(client, conn):
    r = client.post(BATCH, json=batch(event(occurred_at="2026-11-10T10:30:00Z"), event(occurred_at="2026-11-10T11:30:00+01:00")))
    assert r.status_code == 201 and r.json()["duplicate"] == 1 and count(conn) == 1


# --- the single route as the oracle -------------------------------------------------------------------------------

LONG = "\U0001F600" * 200
EQUIVALENCE_SET = [
    event(idempotency_key="new"),
    event(idempotency_key="resend"),
    event(idempotency_key="resend"),
    event(idempotency_key="closed-hour", occurred_at="2026-11-10T09:15:00Z"),               # H - 1 h is closed: late
    event(idempotency_key="boundary-lo", occurred_at="2026-11-10T09:59:59.999999Z"),        # 1 us before H: the closed hour
    event(idempotency_key="boundary-hi", occurred_at="2026-11-10T10:00:00Z"),               # exactly H: open
    event(idempotency_key="boundary-hi-offset", occurred_at="2026-11-10T10:59:59.999999+00:00"),
    event(idempotency_key="offset-into-closed", occurred_at="2026-11-10T10:30:00+01:30"),   # 09:00 UTC: closed
    event(idempotency_key="negative-offset", occurred_at="2026-11-10T04:59:59.999999-05:00"),  # 09:59:59.999999 UTC: closed
    event(idempotency_key="offset-plus-16", occurred_at="2026-11-10T10:30:00+16:00"),        # past Postgres's text limit of 15:59
    event(idempotency_key="offset-minus-23-59", occurred_at="2026-11-10T10:30:00-23:59"),
    event(idempotency_key="year-1", occurred_at="0001-01-01T00:00:00Z"),
    event(idempotency_key="year-9999", occurred_at="9999-12-31T23:59:59.999999Z"),
    event(idempotency_key="year-9999-offset", occurred_at="9999-12-31T23:59:59.999999+23:59"),
    event(idempotency_key="year-1-offset", occurred_at="0001-01-01T00:00:00-23:59"),
    event(idempotency_key="y" * 200, model="m" * 200, tenant_id="t1"),
    event(idempotency_key=LONG, model=LONG),
    event(idempotency_key="ключ-日本語-é", model="модель"),
    event(idempotency_key="zero", tokens_in=0, tokens_out=0),
    event(idempotency_key="big", tokens_in=10**12, tokens_out=10**12),
]


def reset_with_a_closed_hour(conn):
    conn.execute("TRUNCATE usage_events, closed_hours, usage_rollups_hourly RESTART IDENTITY CASCADE")
    conn.commit()
    close_hour(conn, H - timedelta(hours=1))


def test_the_batch_stores_the_same_rows_and_statuses_as_the_single_route(client, conn):
    reset_with_a_closed_hour(conn)
    one_by_one = [{201: "accepted", 200: "duplicate"}[client.post("/v1/usage", json=e).status_code] for e in EQUIVALENCE_SET]
    expected_rows = rows(conn)
    reset_with_a_closed_hour(conn)
    r = client.post(BATCH, json=batch(*EQUIVALENCE_SET))
    assert r.status_code == 201
    assert [x["status"] for x in r.json()["results"]] == one_by_one
    assert rows(conn) == expected_rows                                  # including `late`
    assert sum(x["late"] for x in expected_rows) == 4 and one_by_one.count("duplicate") == 1


@pytest.mark.parametrize("change", [{"model": "m2"}, {"tokens_in": 11}, {"tokens_out": 6}, {"occurred_at": "2026-11-10T10:30:01Z"}])
def test_a_conflicting_key_is_409_on_both_routes(client, conn, change):
    client.post("/v1/usage", json=event())
    assert client.post("/v1/usage", json=event(**change)).status_code == 409
    r = client.post(BATCH, json=batch(event(idempotency_key="fresh"), event(**change)))
    assert r.status_code == 409
    assert "events[1]" in r.json()["detail"]
    assert count(conn) == 1                                                 # "fresh" was rolled back with the batch


# --- all or nothing -----------------------------------------------------------------------------------------------

def test_an_invalid_item_is_a_422_that_names_the_item_stores_nothing_and_echoes_nothing(client, conn):
    secret = "SECRET-VALUE-IN-BODY"
    bad = event(idempotency_key=secret, tokens_in=-1)
    r = client.post(BATCH, json=batch(*events(3), bad))
    assert r.status_code == 422
    assert any(e["loc"][:3] == ["body", "events", 3] for e in r.json()["detail"])
    assert secret not in r.text
    assert count(conn) == 0


@pytest.mark.parametrize("hostile", [
    '{"events": [{"tenant_id": "t1", "idempotency_key": "k", "model": "m", "tokens_in": NaN, "tokens_out": 1, "occurred_at": "2026-11-10T10:30:00Z"}]}',
    '{"events": [{"tenant_id": "t1", "idempotency_key": "\\ud800", "model": "m", "tokens_in": 1, "tokens_out": 1, "occurred_at": "2026-11-10T10:30:00Z"}]}',
    '{"events": [{"tenant_id": "t1", "idempotency_key": "a\\u0000b", "model": "m", "tokens_in": 1, "tokens_out": 1, "occurred_at": "2026-11-10T10:30:00Z"}]}',
    '{"events": [{"tenant_id": "t1", "idempotency_key": "k", "model": "m", "tokens_in": 1, "tokens_out": 1, "occurred_at": 5}]}',
])
def test_hostile_items_are_a_422_not_a_500(client, conn, hostile):
    assert client.post(BATCH, content=hostile, headers=JSON).status_code == 422
    assert count(conn) == 0


def test_an_item_for_another_tenant_is_a_403_that_names_it_and_stores_nothing(client, conn):
    r = client.post(BATCH, json=batch(*events(3), event(idempotency_key="x", tenant_id="t2"), *events(2, start=10)))
    assert r.status_code == 403
    assert "events[3]" in r.json()["detail"] and "t2" not in r.text
    assert count(conn) == 0


def test_the_tenant_check_comes_before_any_database_work(client, conn, monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("the database was opened before the tenant check")

    monkeypatch.setattr(db, "request_connection", fail)
    assert client.post(BATCH, json=batch(event(tenant_id="t2"))).status_code == 403


def test_a_key_already_stored_with_another_payload_is_a_409_naming_the_item_and_stores_nothing(client, conn):
    client.post("/v1/usage", json=event(idempotency_key="old"))
    r = client.post(BATCH, json=batch(*events(3), event(idempotency_key="old", tokens_in=99)))
    assert r.status_code == 409 and "events[3]" in r.json()["detail"]
    assert [x["idempotency_key"] for x in rows(conn)] == ["old"]


def test_a_key_twice_in_the_batch_with_different_payloads_is_a_409_naming_both_and_stores_nothing(client, conn):
    r = client.post(BATCH, json=batch(event(idempotency_key="a"), event(idempotency_key="b"), event(idempotency_key="a", model="other")))
    assert r.status_code == 409 and "events[0] and events[2]" in r.json()["detail"]
    assert count(conn) == 0
    assert client.post(BATCH, json=batch(event(idempotency_key="a"), event(idempotency_key="b"))).status_code == 201   # nothing stuck


def test_a_conflict_log_line_cannot_be_forged_by_the_key(client, conn, caplog):
    client.post("/v1/usage", json=event(idempotency_key="a\nFAKE LINE"))
    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        client.post(BATCH, json=batch(event(idempotency_key="a\nFAKE LINE", model="other")))
    assert "a\nFAKE LINE" not in caplog.text and "usage batch conflict" in caplog.text


# --- hours and `late` ---------------------------------------------------------------------------------------------

def test_events_in_a_closed_hour_are_late_and_stay_out_of_the_closed_rollup(client, conn):
    assert client.post("/v1/usage", json=event(idempotency_key="before")).status_code == 201
    assert close_hour(conn, H) == (1, 1)
    r = client.post(BATCH, json=batch(event(idempotency_key="after-1"), event(idempotency_key="after-2", occurred_at="2026-11-10T10:59:59Z"),
                                      event(idempotency_key="open", occurred_at="2026-11-10T11:00:00Z")))
    assert [x["late"] for x in r.json()["results"]] == [True, True, False]
    assert conn.execute("SELECT sum(event_count) AS n FROM usage_rollups_hourly").fetchone()["n"] == 1
    again = client.post(BATCH, json=batch(event(idempotency_key="after-1")))
    assert again.json()["results"][0]["late"] is True                    # a duplicate reports the stored flag


def test_a_batch_racing_the_close_is_never_both_and_never_neither(conn):
    """The ingest-vs-close_hour race of tests/test_attack_ingest.py, over the batch route."""
    app.dependency_overrides[require_entitlement] = lambda: "t1"
    rounds, senders, batches, per_batch = 12, 5, 2, 4
    rng = random.Random(11)
    try:
        for r in range(rounds):
            hour = H + timedelta(hours=r)
            start = threading.Barrier(senders + 1)
            delay = rng.uniform(0, 0.03)

            def send(s):
                client = TestClient(app, raise_server_exceptions=False)
                start.wait(timeout=30)
                return [client.post(BATCH, json=batch(*[event(
                    idempotency_key=f"r{r}-s{s}-b{b}-{i}", occurred_at=(hour + timedelta(minutes=5 + i)).isoformat())
                    for i in range(per_batch)])).status_code for b in range(batches)]

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
            assert statuses == [201] * (senders * batches)
            total = senders * batches * per_batch
            late = conn.execute("SELECT count(*) AS n FROM usage_events WHERE occurred_at >= %s AND late", (hour,)).fetchone()["n"]
            stored = conn.execute("SELECT count(*) AS n FROM usage_events WHERE occurred_at >= %s", (hour,)).fetchone()["n"]
            counted = conn.execute("SELECT coalesce(sum(event_count), 0) AS n FROM usage_rollups_hourly WHERE hour_start = %s",
                                   (hour,)).fetchone()["n"]
            conn.commit()
            assert counted + late == stored == total, f"round {r}: counted={counted} late={late} stored={stored}"
    finally:
        app.dependency_overrides.clear()


def test_a_batch_spanning_two_hours_racing_a_close_of_the_second_is_never_both_and_never_neither(conn):
    """The lock of every hour counts, not only the first one's (the earlier hour is never closed here)."""
    app.dependency_overrides[require_entitlement] = lambda: "t1"
    rounds, senders = 12, 5
    rng = random.Random(5)
    try:
        for r in range(rounds):
            hour = H + timedelta(hours=10 * r)                       # the batches span `hour` and `hour + 1`
            start = threading.Barrier(senders + 1)
            delay = rng.uniform(0, 0.03)

            def send(s):
                client = TestClient(app, raise_server_exceptions=False)
                start.wait(timeout=30)
                return client.post(BATCH, json=batch(
                    event(idempotency_key=f"r{r}-s{s}-lo", occurred_at=(hour + timedelta(minutes=10)).isoformat()),
                    event(idempotency_key=f"r{r}-s{s}-hi", occurred_at=(hour + timedelta(hours=1, minutes=10)).isoformat()))).status_code

            def close():
                start.wait(timeout=30)
                closer = db.connect()
                try:
                    time.sleep(delay)
                    return close_hour(closer, hour + timedelta(hours=1))
                finally:
                    closer.close()

            with ThreadPoolExecutor(max_workers=senders + 1) as pool:
                sends = [pool.submit(send, s) for s in range(senders)]
                closing = pool.submit(close)
                assert [f.result() for f in sends] == [201] * senders
                closing.result()
            hi = hour + timedelta(hours=1)
            late = conn.execute("SELECT count(*) AS n FROM usage_events WHERE occurred_at >= %s AND late", (hi,)).fetchone()["n"]
            counted = conn.execute("SELECT coalesce(sum(event_count), 0) AS n FROM usage_rollups_hourly WHERE hour_start = %s",
                                   (hi,)).fetchone()["n"]
            conn.commit()
            assert counted + late == senders, f"round {r}: counted={counted} late={late}"
    finally:
        app.dependency_overrides.clear()


def test_overlapping_batches_with_their_keys_in_opposite_order_both_finish(conn):
    app.dependency_overrides[require_entitlement] = lambda: "t1"
    try:
        for r in range(4):
            keys = [f"r{r}-{i:04d}" for i in range(400)]
            start = threading.Barrier(2)

            def send(order):
                client = TestClient(app, raise_server_exceptions=False)
                body = batch(*[event(idempotency_key=k) for k in order])
                start.wait(timeout=30)
                return client.post(BATCH, json=body).status_code

            with ThreadPoolExecutor(max_workers=2) as pool:
                forward, backward = pool.submit(send, keys), pool.submit(send, keys[::-1])
                codes = sorted([forward.result(timeout=60), backward.result(timeout=60)])
            assert codes in ([200, 201], [201, 201]), f"round {r}: {codes}"
            assert conn.execute("SELECT count(*) AS n FROM usage_events WHERE idempotency_key LIKE %s", (f"r{r}-%",)).fetchone()["n"] == 400
            conn.commit()
    finally:
        app.dependency_overrides.clear()


def test_twenty_identical_batches_store_each_event_once(client, conn):
    start = threading.Barrier(20)

    def send(_):
        c = TestClient(app, raise_server_exceptions=False)
        start.wait(timeout=30)
        return c.post(BATCH, json=batch(*events(30))).json()

    with ThreadPoolExecutor(max_workers=20) as pool:
        answers = [f.result() for f in [pool.submit(send, i) for i in range(20)]]
    assert sum(a["accepted"] for a in answers) == 30 and sum(a["duplicate"] for a in answers) == 20 * 30 - 30
    assert count(conn) == 30


# --- locks, timeouts, a dying database ----------------------------------------------------------------------------

def test_a_batch_whose_hour_is_being_closed_gives_up_with_503_stores_nothing_and_the_resend_works(client, conn, short_wait):
    holder = db.connect()
    try:
        holder.execute("SELECT pg_advisory_xact_lock(%s, %s)", (HOUR_LOCK_CLASS, hour_index(H + timedelta(hours=1))))
        started = time.monotonic()
        r = client.post(BATCH, json=batch(event(idempotency_key="free", occurred_at=H.isoformat()),
                                          event(idempotency_key="held", occurred_at=(H + timedelta(hours=1, minutes=5)).isoformat())))
        waited = time.monotonic() - started
    finally:
        holder.rollback()
        holder.close()
    assert (r.status_code, r.json(), r.headers.get("retry-after")) == (503, {"detail": "database unavailable"}, "1")
    assert 0.25 <= waited < 3
    assert count(conn) == 0
    assert client.post(BATCH, json=batch(event(idempotency_key="held"))).status_code == 201


def test_a_batch_statement_past_the_timeout_is_a_504_and_stores_nothing(client, conn, monkeypatch):
    monkeypatch.setattr(db, "REQUEST_STATEMENT_TIMEOUT_MS", 300)
    original = ingest.lock_hour_shared

    def slow(c, *args, **kwargs):
        c.execute("SELECT pg_sleep(5)")
        return original(c, *args, **kwargs)

    monkeypatch.setattr(ingest, "lock_hour_shared", slow)
    started = time.monotonic()
    r = client.post(BATCH, json=batch(*events(3)))
    assert (r.status_code, r.json()) == (504, {"detail": "query timed out"})
    assert time.monotonic() - started < 4 and count(conn) == 0
    monkeypatch.setattr(ingest, "lock_hour_shared", original)
    assert client.post(BATCH, json=batch(*events(3))).status_code == 201


def test_the_database_unreachable_is_503(client, monkeypatch):
    def refuse(*args, **kwargs):
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr(db, "request_connection", refuse)
    r = client.post(BATCH, json=batch(event()))
    assert (r.status_code, r.json(), r.headers.get("retry-after")) == (503, {"detail": "database unavailable"}, "1")


def test_the_database_dying_mid_batch_is_503_stores_nothing_and_the_resend_works(client, conn):
    holder = db.connect()                       # a close in progress: the batch waits for the hour's exclusive lock
    try:
        holder.execute("SELECT pg_advisory_xact_lock(%s, %s)", (HOUR_LOCK_CLASS, hour_index(H)))
        with ThreadPoolExecutor(max_workers=1) as pool:
            posting = pool.submit(client.post, BATCH, json=batch(*events(5)))
            try:
                wait_until_blocked(conn)
                conn.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted"
                    " AND database = (SELECT oid FROM pg_database WHERE datname = current_database())")
                conn.commit()
                assert posting.result(timeout=10).status_code == 503
            finally:
                holder.rollback()
    finally:
        holder.rollback()
        holder.close()
    assert count(conn) == 0
    assert client.post(BATCH, json=batch(*events(5))).status_code == 201


def test_the_database_killed_after_some_hours_are_locked_stores_nothing(client, conn):
    """Killed while the batch holds its first hour's lock and waits for the second."""
    holder = db.connect()
    try:
        holder.execute("SELECT pg_advisory_xact_lock(%s, %s)", (HOUR_LOCK_CLASS, hour_index(H + timedelta(hours=1))))
        with ThreadPoolExecutor(max_workers=1) as pool:
            posting = pool.submit(client.post, BATCH, json=batch(
                event(idempotency_key="a", occurred_at=H.isoformat()), event(idempotency_key="b", occurred_at=(H + timedelta(hours=1)).isoformat())))
            try:
                wait_until_blocked(conn)
                conn.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted"
                    " AND database = (SELECT oid FROM pg_database WHERE datname = current_database())")
                conn.commit()
                assert posting.result(timeout=10).status_code == 503
            finally:
                holder.rollback()
    finally:
        holder.rollback()
        holder.close()
    assert count(conn) == 0 and blocked(conn, "advisory") == 0


def test_every_hour_lock_is_its_own_statement_in_ascending_order_before_the_insert(client, conn, monkeypatch):
    seen = []
    original = ingest.lock_hour_shared
    monkeypatch.setattr(ingest, "lock_hour_shared", lambda c, ts: (seen.append(("lock", hour_index(ts))), original(c, ts)))
    real_execute = psycopg.Connection.execute

    def spy(self, query, *args, **kwargs):
        if isinstance(query, str) and query.lstrip().startswith("INSERT INTO usage_events"):
            seen.append(("insert",))
        return real_execute(self, query, *args, **kwargs)

    monkeypatch.setattr(psycopg.Connection, "execute", spy)
    hours = [H + timedelta(hours=h) for h in (5, 1, 3, 1)]
    r = client.post(BATCH, json=batch(*[event(idempotency_key=f"k{i}", occurred_at=h.isoformat()) for i, h in enumerate(hours)]))
    assert r.status_code == 201
    assert seen == [("lock", hour_index(H) + n) for n in (1, 3, 5)] + [("insert",)]


# --- limits -------------------------------------------------------------------------------------------------------

def test_a_body_that_is_not_json_is_a_422(client, conn):
    assert client.post(BATCH, content=b"[1, 2", headers=JSON).status_code == 422 and count(conn) == 0


def test_the_limits_are_the_documented_numbers():
    assert (ingest.MAX_BATCH_EVENTS, ingest.MAX_BATCH_HOURS) == (1000, 48)


def test_1001_events_is_a_422_and_1000_is_not(client, conn):
    assert client.post(BATCH, json=batch(*events(1001))).status_code == 422
    assert count(conn) == 0
    assert client.post(BATCH, json=batch(*events(1000))).status_code == 201


def test_49_distinct_hours_is_a_422_and_48_is_not(client, conn):
    def spread(n):
        return batch(*[event(idempotency_key=f"k{i}", occurred_at=(H + timedelta(hours=i % n, minutes=i // n)).isoformat()) for i in range(96)])

    r = client.post(BATCH, json=spread(49))
    assert r.status_code == 422 and "49 UTC hours" in r.text and count(conn) == 0
    assert client.post(BATCH, json=spread(48)).status_code == 201 and count(conn) == 96


def test_hours_are_counted_in_utc_not_in_the_offset_the_client_wrote(client, conn):
    same_instant_hours = [event(idempotency_key=f"k{i}", occurred_at=f"2026-11-10T{10 + i % 5:02d}:30:00+0{i % 5}:00") for i in range(60)]
    assert client.post(BATCH, json=batch(*same_instant_hours)).status_code == 201     # every one is 10:30 UTC: one hour


@pytest.mark.parametrize("body", [{"events": []}, {}, {"events": "x"}, {"events": {"a": 1}}, {"events": None}, [event()],
                                  {"events": [None]}, {"events": [[]]}])
def test_an_empty_missing_or_malformed_events_list_is_a_422(client, conn, body):
    r = client.post(BATCH, content=json.dumps(body).encode(), headers=JSON)
    assert r.status_code == 422 and count(conn) == 0


def maximal_events():
    """MAX_BATCH_EVENTS events of the longest valid spelling: 200 non-BMP characters in each text field, distinct keys."""
    out = []
    for i in range(ingest.MAX_BATCH_EVENTS):
        key = chr(0x1F600 + i // 40) + chr(0x1F600 + i % 40) + "\U0001F600" * 198
        out.append({"tenant_id": "\U0001F600" * 200, "idempotency_key": key, "model": "\U0001F600" * 200,
                    "tokens_in": 10**12, "tokens_out": 10**12, "occurred_at": "9999-12-31T23:59:59.999999+23:59"})
    return out


def test_the_cap_is_the_size_of_a_thousand_of_the_largest_valid_events():
    body = json.dumps({"events": maximal_events()}).encode()            # ASCII-escaped, default separators
    assert len(body) == ingest.MAX_BATCH_BODY_BYTES
    assert len(ingest.UsageBatch.model_validate_json(body).events) == ingest.MAX_BATCH_EVENTS     # every one of them valid
    compact = json.dumps({"events": maximal_events()}, separators=(",", ":"), ensure_ascii=False).encode()
    assert len(compact) < ingest.MAX_BATCH_BODY_BYTES                                             # other encodings are smaller


def test_a_body_of_exactly_the_cap_is_201_and_one_byte_more_is_413(client, conn):
    """The cap counts bytes, not events. (A real insert of 1000 of the largest events is not tried: their 200-character
    emoji keys and tenant ids make Postgres's text comparisons in the unique index take ~40 ms a row, so that batch would
    answer 504 past the 15 s statement timeout. See "Limits" in docs/ingest.md.)"""
    raw = json.dumps(batch(*events(3))).encode()
    padded = raw + b" " * (ingest.MAX_BATCH_BODY_BYTES - len(raw))
    assert client.post(BATCH, content=padded, headers=JSON).status_code == 201
    assert client.post(BATCH, content=padded + b" ", headers=JSON).status_code == 413
    assert client.post(BATCH, content=(padded[i:i + 65536] for i in range(0, len(padded) + 1, 65536)), headers=JSON).status_code == 200
    assert count(conn) == 3


def test_the_largest_valid_events_are_stored_resent_and_rolled_up_by_a_batch(conn):
    app.dependency_overrides[require_entitlement] = lambda: "\U0001F600" * 200
    try:
        client = TestClient(app, raise_server_exceptions=False)
        body = {"events": maximal_events()[:10]}
        r = client.post(BATCH, json=body)
        assert (r.status_code, r.json()["accepted"]) == (201, 10)
        assert client.post(BATCH, json=body).status_code == 200
        assert count(conn) == 10
    finally:
        app.dependency_overrides.clear()


def test_the_body_cap_is_a_413_before_parsing_and_before_the_edge(conn):
    cap = ingest.MAX_BATCH_BODY_BYTES
    client = TestClient(app, raise_server_exceptions=False)                     # no override: no credentials
    declared = client.post(BATCH, content=b"{}", headers={**JSON, "content-length": str(cap + 1)})
    assert (declared.status_code, declared.json()) == (413, {"detail": "request body too large"})
    streamed = client.post(BATCH, content=(b" " * 65536 for _ in range(cap // 65536 + 2)), headers=JSON)   # no Content-Length
    assert streamed.status_code == 413
    assert client.post(BATCH, content=b" " * (cap + 1), headers=JSON).status_code == 413
    assert client.post(BATCH, json=batch(event())).status_code == 401           # a small body reaches the edge
    assert count(conn) == 0


def test_the_single_routes_cap_is_unchanged(conn):
    assert ingest.MAX_BODY_BYTES == 64 * 1024
    client = TestClient(app, raise_server_exceptions=False)
    assert client.post("/v1/usage", content=b" " * (ingest.MAX_BODY_BYTES + 1), headers=JSON).status_code == 413


def test_every_post_route_with_a_body_has_a_body_cap():
    """A path missing from BodyLimit's `limits` has no cap at all. The webhook caps its own body (webhooks.raw_body)."""
    limits = next(m.kwargs["limits"] for m in app.user_middleware if m.kwargs.get("limits") is not None)
    own_cap = {"/v1/stripe/webhook"}
    posts = {path for path, ops in app.openapi()["paths"].items() if "post" in ops}
    assert BATCH in posts and posts - own_cap <= set(limits), sorted(posts - own_cap - set(limits))
    assert limits[BATCH] == ingest.MAX_BATCH_BODY_BYTES


def test_both_routes_decide_a_resend_with_the_same_comparison():
    stored = {"model": "m1", "tokens_in": 10, "tokens_out": 5, "occurred_at": ingest.UsageEvent(**event()).occurred_at}
    same = ingest.UsageEvent(**event(occurred_at="2026-11-10T11:30:00+01:00"))
    assert ingest._same_payload(stored, same)
    for change in ({"model": "m2"}, {"tokens_in": 11}, {"tokens_out": 6}, {"occurred_at": "2026-11-10T10:30:00.000001Z"}):
        assert not ingest._same_payload(stored, ingest.UsageEvent(**event(**change)))


@pytest.mark.parametrize("route", ["/v1/usage", BATCH])
def test_each_route_answers_a_changed_payload_with_409_and_an_unchanged_one_with_a_duplicate(client, conn, route):
    wrap = (lambda e: e) if route == "/v1/usage" else (lambda e: batch(e))
    assert client.post(route, json=wrap(event())).status_code == 201
    assert client.post(route, json=wrap(event())).status_code == 200
    assert client.post(route, json=wrap(event(tokens_in=11))).status_code == 409
    assert count(conn) == 1


def test_a_batch_that_repeats_a_key_with_a_different_payload_never_reaches_the_database(client, conn, monkeypatch):
    monkeypatch.setattr(db, "request_connection", lambda *a, **k: pytest.fail("the database was opened"))
    r = client.post(BATCH, json=batch(event(), event(idempotency_key="b"), event(tokens_in=99)))
    assert r.status_code == 409 and "events[0] and events[2]" in r.json()["detail"]


def test_the_first_copy_of_a_repeated_key_is_the_one_stored(client, conn):
    r = client.post(BATCH, json=batch(event(occurred_at="2026-11-10T11:30:00+01:00"), event(occurred_at="2026-11-10T10:30:00Z")))
    assert [x["status"] for x in r.json()["results"]] == ["accepted", "duplicate"]
    assert rows(conn)[0]["occurred_at"].isoformat() == "2026-11-10T10:30:00+00:00" and count(conn) == 1
