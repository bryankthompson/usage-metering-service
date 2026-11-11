"""A request that waits for a lock gives up after db.LOCK_WAIT with 503 + Retry-After, instead of pinning a worker thread
for as long as the lock is held (RT-concurrency-2: ~40 waiters on one hour's lock hung /healthz and every other hour).

Each test holds a lock from a second connection. A safety timer releases it after a few seconds, so that without the
timeout a test fails on a wrong answer instead of hanging.
"""
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app import db
from app.edge import require_entitlement
from app.main import app
from app.rollup import HOUR_LOCK_CLASS, close_hour, hour_index
from tests.stripe_signing import SECRET, payment_failed_event, subscription_event
from tests.test_attack_ingest import blocked, count, event, wait_until_blocked
from tests.test_webhooks import T, post, row

H = datetime(2026, 11, 10, 10, tzinfo=timezone.utc)
OTHER_HOUR = "2026-11-10T14:30:00Z"
HOUR_LOCK = ("SELECT pg_advisory_xact_lock(%s, %s)", (HOUR_LOCK_CLASS, hour_index(H)))


@contextmanager
def held(sql: str, params: tuple, release_after: float = 5.0):
    holder = db.connect()
    try:
        holder.execute(sql, params)
        safety = threading.Timer(release_after, holder.rollback)
        safety.start()
        try:
            yield holder
        finally:
            safety.cancel()
            holder.rollback()
    finally:
        holder.close()


@pytest.fixture
def client(conn):
    app.dependency_overrides[require_entitlement] = lambda: "t1"
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()


@pytest.fixture
def short_wait(monkeypatch):
    monkeypatch.setattr(db, "LOCK_WAIT", "300ms", raising=False)


def test_the_wait_is_two_seconds_and_a_constant():
    assert db.LOCK_WAIT == "2s"


def test_ingest_gives_up_on_a_held_hour_lock_with_503_and_a_resend_is_stored_once(client, conn, short_wait):
    with held(*HOUR_LOCK):
        started = time.monotonic()
        r = client.post("/v1/usage", json=event())
        waited = time.monotonic() - started
    assert (r.status_code, r.json(), r.headers.get("retry-after")) == (503, {"detail": "database unavailable"}, "1")
    assert 0.25 <= waited < 3
    assert count(conn) == 0                                                       # the failed attempt stored nothing
    assert client.post("/v1/usage", json=event()).status_code == 201 and count(conn) == 1
    assert client.post("/v1/usage", json=event()).status_code == 200 and count(conn) == 1


def test_many_waiters_on_one_hour_do_not_hang_healthz_or_another_hour(client, conn, monkeypatch):
    monkeypatch.setattr(db, "LOCK_WAIT", "400ms", raising=False)
    with held(*HOUR_LOCK, release_after=6.0), TestClient(app, raise_server_exceptions=False) as shared:  # one shared thread pool
        with ThreadPoolExecutor(max_workers=60) as pool:
            waiters = [pool.submit(shared.post, "/v1/usage", json=event(idempotency_key=f"k{i}")) for i in range(60)]
            wait_until_blocked(conn)
            started = time.monotonic()
            health = shared.get("/healthz")
            other = shared.post("/v1/usage", json=event(idempotency_key="elsewhere", occurred_at=OTHER_HOUR))
            answered_in = time.monotonic() - started
            assert {w.result(timeout=15).status_code for w in waiters} == {503}
    assert (health.status_code, other.status_code) == (200, 201)
    assert answered_in < 3, "healthz and another hour waited for the held lock"


def test_healthz_answers_while_every_worker_thread_is_parked_in_the_database(client, conn, monkeypatch):
    """/healthz touches nothing, so it must not queue behind the ~40 threads the database routes use (RT-operations: with
    50 requests parked in a query on a frozen database it stopped answering). 50 senders wait on a held hour lock with a
    long wait; the lock is released after 5 s, so a /healthz that queues answers after about 5 s instead of at once."""
    monkeypatch.setattr(db, "LOCK_WAIT", "30s", raising=False)
    with held(*HOUR_LOCK, release_after=5.0), TestClient(app, raise_server_exceptions=False) as shared:
        with ThreadPoolExecutor(max_workers=50) as pool:
            senders = [pool.submit(shared.post, "/v1/usage", json=event(idempotency_key=f"k{i}")) for i in range(50)]
            for _ in range(500):                                  # all 40 pool threads are inside the database wait
                if blocked(conn, "advisory") >= 40:
                    break
                time.sleep(0.01)
            else:
                pytest.fail("the worker pool never filled with waiting requests")
            started = time.monotonic()
            health = shared.get("/healthz")
            answered_in = time.monotonic() - started
            statuses = {s.result(timeout=20).status_code for s in senders}
    assert health.status_code == 200 and health.json() == {"status": "ok"}
    assert answered_in < 1, f"/healthz waited {answered_in:.1f} s behind requests parked in the database"
    assert statuses == {201}                                      # the parked requests finished normally once the lock went


@pytest.fixture
def webhook_env(monkeypatch):
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", SECRET)
    monkeypatch.delenv("GRACE_PERIOD_HOURS", raising=False)
    return TestClient(app, raise_server_exceptions=False)


def test_a_webhook_gives_up_on_a_held_tenant_lock_and_the_redelivery_is_applied_once(webhook_env, conn, short_wait):
    with held("SELECT pg_advisory_xact_lock(hashtext(%s))", ("tenant-1",)):
        started = time.monotonic()
        r = post(webhook_env, subscription_event("evt_1", T))
        waited = time.monotonic() - started
    assert (r.status_code, r.json(), r.headers.get("retry-after")) == (503, {"detail": "database unavailable"}, "1")
    assert 0.25 <= waited < 3
    assert conn.execute("SELECT (SELECT count(*) FROM entitlements) AS e, (SELECT count(*) FROM stripe_events) AS s"
                        ).fetchone() == {"e": 0, "s": 0}                          # nothing of the failed attempt remains
    assert post(webhook_env, subscription_event("evt_1", T)).json() == {"status": "processed"}
    assert post(webhook_env, subscription_event("evt_1", T)).json() == {"status": "duplicate"}


def test_a_payment_failure_gives_up_on_a_held_entitlement_row_lock(webhook_env, conn, short_wait):
    post(webhook_env, subscription_event("evt_1", T))
    with held("SELECT * FROM entitlements WHERE tenant_id = %s FOR UPDATE", ("tenant-1",)):
        r = post(webhook_env, payment_failed_event("evt_2", T + 10))
    assert (r.status_code, r.headers.get("retry-after")) == (503, "1")
    conn.rollback()
    assert row(conn)["status"] == "active"
    assert post(webhook_env, payment_failed_event("evt_2", T + 10)).json() == {"status": "processed"}


def test_the_closing_side_has_no_timeout_it_waits_for_ingests_in_flight(conn, monkeypatch):
    monkeypatch.setattr(db, "LOCK_WAIT", "100ms", raising=False)
    closer = db.connect()
    try:
        with held("SELECT pg_advisory_xact_lock_shared(%s, %s)", (HOUR_LOCK_CLASS, hour_index(H))) as ingest_in_flight:
            with ThreadPoolExecutor(max_workers=1) as pool:
                closing = pool.submit(close_hour, closer, H)
                time.sleep(0.6)                                                   # six times the ingest side's wait
                assert not closing.done(), "close_hour gave up waiting for an ingest in flight"
                ingest_in_flight.rollback()
                assert closing.result(timeout=5) == (0, 0)
    finally:
        closer.close()


def test_the_limit_ends_with_the_transaction(conn, monkeypatch):
    # A connection reused after a request (or by the rollup) must not keep a lock timeout it was never meant to have.
    monkeypatch.setattr(db, "LOCK_WAIT", "300ms", raising=False)
    db.limit_lock_wait(conn)
    assert conn.execute("SHOW lock_timeout").fetchone()["lock_timeout"] == "300ms"
    conn.commit()
    assert conn.execute("SHOW lock_timeout").fetchone()["lock_timeout"] == "0"
