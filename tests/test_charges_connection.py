"""GET /v1/tenants/{id}/charges takes its database connection inside the handler, as POST /v1/usage does.

A dependency's connection stays open while the request waits for a free worker thread (the note in `ingest_usage`), so a
burst of charges requests used up the database's connection slots. And a refused request (wrong tenant, a 422) has no
reason to open a connection at all."""
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import psycopg
import pytest
from fastapi.testclient import TestClient

from app import db, pricing
from app.edge import require_entitlement
from app.main import app

T0 = datetime(2026, 11, 1, tzinfo=timezone.utc)
GOOD = {"start": T0.isoformat(), "end": (T0 + timedelta(hours=3)).isoformat()}


@pytest.fixture
def client():
    app.dependency_overrides[require_entitlement] = lambda: "tenant-1"
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def connects(monkeypatch):
    """Every `db.connect()` call made while the test runs (the edge is overridden, so these are the handler's)."""
    calls = []
    real = db.connect

    def counting(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(db, "connect", counting)
    return calls


REFUSED = [
    pytest.param("tenant-2", GOOD, 403, id="another tenant"),
    pytest.param("tenant-1", {"start": "2026-11-01T00:00:00", "end": "2026-11-01T03:00:00"}, 422, id="naive datetimes"),
    pytest.param("tenant-1", {"start": GOOD["end"], "end": GOOD["start"]}, 422, id="range runs backwards"),
    pytest.param("tenant-1", {"start": GOOD["start"], "end": (T0 + timedelta(days=400)).isoformat()}, 422, id="range too wide"),
    pytest.param("tenant-1", {"start": "5", "end": GOOD["end"]}, 422, id="start is not ISO text"),
]


@pytest.mark.parametrize(("tenant", "params", "status"), REFUSED)
def test_a_refused_charges_request_opens_no_database_connection(client, conn, connects, tenant, params, status):
    assert client.get(f"/v1/tenants/{tenant}/charges", params=params).status_code == status
    assert connects == []


def test_an_answered_charges_request_opens_exactly_one_connection(client, conn, connects):
    """The control for the test above: the counter does see the handler's connection."""
    assert client.get("/v1/tenants/tenant-1/charges", params=GOOD).status_code == 200
    assert len(connects) == 1


def test_a_missing_price_is_still_409_and_closes_its_connection(client, conn, connects):
    conn.execute("INSERT INTO usage_rollups_hourly (tenant_id, model, hour_start, tokens_in, tokens_out, event_count)"
                 " VALUES ('tenant-1', 'unpriced', %s, 1, 1, 1)", (T0,))
    conn.commit()
    resp = client.get("/v1/tenants/tenant-1/charges", params=GOOD)
    assert resp.status_code == 409 and "no price for model 'unpriced'" in resp.json()["detail"]
    assert len(connects) == 1


def test_a_database_that_cannot_be_reached_is_503_with_retry_after_1(client, monkeypatch):
    def refuse(*args, **kwargs):
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr(db, "connect", refuse)
    resp = client.get("/v1/tenants/tenant-1/charges", params=GOOD)
    assert (resp.status_code, resp.json(), resp.headers["retry-after"]) == (503, {"detail": "database unavailable"}, "1")


def open_connections(conn) -> int:
    """Sessions on this database other than the test's own. The commit first: pg_stat_activity is a snapshot that is kept
    until the transaction ends, so without it every poll would repeat the first answer."""
    conn.commit()
    return conn.execute("SELECT count(*) AS n FROM pg_stat_activity WHERE datname = current_database()"
                        " AND pid <> pg_backend_pid()").fetchone()["n"]


def test_a_burst_larger_than_the_worker_pool_holds_no_connection_for_a_request_that_has_no_thread(conn, monkeypatch):
    """60 requests, more than the ~40 worker threads, all stalled inside the read. Only a request that holds a thread may
    hold a connection: with the connection taken by a dependency, the requests still waiting for a thread held one too."""
    release = threading.Event()
    lock = threading.Lock()
    inside = []
    real = pricing.unclosed_hours

    def stalled(*args, **kwargs):
        with lock:
            inside.append(1)
        release.wait(timeout=30)
        return real(*args, **kwargs)

    monkeypatch.setattr(pricing, "unclosed_hours", stalled)
    app.dependency_overrides[require_entitlement] = lambda: "tenant-1"
    try:
        with TestClient(app) as shared, ThreadPoolExecutor(max_workers=60) as pool:
            senders = [pool.submit(shared.get, "/v1/tenants/tenant-1/charges", params=GOOD) for _ in range(60)]
            seen, steady = (-1, -1), 0
            for _ in range(600):                                     # until the number of stalled requests stops growing
                time.sleep(0.05)
                with lock:
                    now = (len(inside), open_connections(conn))
                steady = steady + 1 if now == seen and now[0] > 0 else 0
                if steady >= 6:                                      # unchanged for 0.3 s: the pool is full
                    break
                seen = now
            else:
                pytest.fail(f"the burst never settled: {seen}")
            stalled_requests, connections = now
            release.set()
            statuses = {s.result(timeout=30).status_code for s in senders}
    finally:
        release.set()
        app.dependency_overrides.clear()
    assert stalled_requests < 60, "the worker pool was never full, so this test shows nothing"
    assert connections == stalled_requests, (
        f"{connections} connections open for {stalled_requests} requests that hold a worker thread")
    assert statuses == {200}
