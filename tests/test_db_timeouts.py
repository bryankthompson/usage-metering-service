"""A database that stops answering must be a 503 within seconds, not a hang that fills the thread pool."""
import socket
import threading
import time
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app import db, pricing, rollup
from app.main import app

BODY = {"tenant_id": "t1", "idempotency_key": "k1", "model": "m1", "tokens_in": 1, "tokens_out": 1,
        "occurred_at": "2026-11-10T10:30:00Z"}


@pytest.fixture
def silent_database():
    """A TCP listener that accepts connections and never answers: what a paused or partitioned database looks like."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(16)
    held = []
    stop = threading.Event()

    def accept():
        server.settimeout(0.1)
        while not stop.is_set():
            try:
                held.append(server.accept()[0])
            except OSError:
                pass

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    yield f"postgresql://metering:metering@127.0.0.1:{server.getsockname()[1]}/metering"
    stop.set()
    thread.join()
    for sock in held:
        sock.close()
    server.close()


def test_a_database_that_never_answers_is_a_503_and_healthz_keeps_answering(silent_database, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", silent_database)
    monkeypatch.setattr(db, "CONNECT_TIMEOUT_S", 1)
    client = TestClient(app, raise_server_exceptions=False)
    result = {}

    def post():
        started = time.monotonic()
        result["resp"] = client.post("/v1/usage", json=BODY, headers={"X-Tenant-Id": "t1"})
        result["seconds"] = time.monotonic() - started

    thread = threading.Thread(target=post)
    thread.start()
    time.sleep(0.3)  # the request is now stuck waiting for the database
    assert client.get("/healthz").status_code == 200
    thread.join(timeout=10)
    assert not thread.is_alive(), "the request hung past the connect timeout"
    assert result["resp"].status_code == 503
    assert result["seconds"] < 5


def test_a_statement_past_the_timeout_is_a_503_not_a_500_and_not_a_hang(conn, monkeypatch):
    monkeypatch.setattr(db, "REQUEST_STATEMENT_TIMEOUT_MS", 300)
    conn.execute("INSERT INTO entitlements (tenant_id, status) VALUES ('t1', 'active')")
    conn.commit()
    original = pricing.charge_lines

    def slow(c, *args, **kwargs):
        c.execute("SELECT pg_sleep(5)")
        return original(c, *args, **kwargs)

    monkeypatch.setattr(pricing, "charge_lines", slow)
    client = TestClient(app, raise_server_exceptions=False)
    started = time.monotonic()
    resp = client.get("/v1/tenants/t1/charges", params={"start": "2026-11-01T00:00:00Z", "end": "2026-11-02T00:00:00Z"},
                      headers={"X-Tenant-Id": "t1"})
    assert (resp.status_code, resp.json()) == (503, {"detail": "database unavailable"})
    assert time.monotonic() - started < 4


def test_the_default_connection_has_the_request_statement_timeout():
    with db.connect() as c:
        assert c.execute("SHOW statement_timeout").fetchone()["statement_timeout"] == "15s"


def test_a_connection_can_opt_out_of_the_statement_timeout():
    with db.connect(statement_timeout_ms=0) as c:
        assert c.execute("SHOW statement_timeout").fetchone()["statement_timeout"] == "0"


def test_the_rollup_connection_has_no_statement_timeout(conn, monkeypatch):
    seen = []

    def spy(c, now, grace=rollup.DEFAULT_GRACE, failed=None):
        seen.append(c.execute("SHOW statement_timeout").fetchone()["statement_timeout"])
        return []

    monkeypatch.setattr(rollup, "close_due_hours", spy)
    rollup.run_once()
    assert seen == ["0"]
