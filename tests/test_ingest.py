import logging
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app.edge import require_entitlement
from app.main import app


@pytest.fixture
def client(conn):
    """A client whose edge check admits tenant-1, on an empty migrated database."""
    app.dependency_overrides[require_entitlement] = lambda: "tenant-1"
    yield TestClient(app)
    app.dependency_overrides.clear()


def event(**changes):
    base = {
        "tenant_id": "tenant-1", "idempotency_key": "key-1", "model": "model-a",
        "tokens_in": 10, "tokens_out": 5, "occurred_at": "2026-11-10T10:30:00Z",
    }
    return {**base, **changes}


def count_events(conn):
    return conn.execute("SELECT count(*) AS n FROM usage_events").fetchone()["n"]


def test_new_event_is_accepted_and_stored(client, conn, caplog):
    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        r = client.post("/v1/usage", json=event())
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "accepted"
    row = conn.execute("SELECT * FROM usage_events WHERE id = %s", (body["event_id"],)).fetchone()
    assert (row["tenant_id"], row["idempotency_key"], row["model"]) == ("tenant-1", "key-1", "model-a")
    assert (row["tokens_in"], row["tokens_out"], row["late"]) == (10, 5, False)
    assert "usage accepted" in caplog.text


def test_resend_is_counted_once(client, conn):
    first = client.post("/v1/usage", json=event())
    again = client.post("/v1/usage", json=event())
    assert (first.status_code, again.status_code) == (201, 200)
    assert first.json()["late"] is False
    assert again.json() == {"status": "duplicate", "event_id": first.json()["event_id"], "late": False}
    assert count_events(conn) == 1


def test_resend_with_same_instant_in_another_offset_is_a_duplicate(client, conn):
    client.post("/v1/usage", json=event(occurred_at="2026-11-10T10:30:00Z"))
    r = client.post("/v1/usage", json=event(occurred_at="2026-11-10T11:30:00+01:00"))
    assert r.status_code == 200
    assert count_events(conn) == 1


@pytest.mark.parametrize("change", [
    {"model": "model-b"},
    {"tokens_in": 11},
    {"tokens_out": 6},
    {"occurred_at": "2026-11-10T10:30:01Z"},
])
def test_same_key_with_a_changed_payload_is_409_and_changes_nothing(client, conn, change):
    client.post("/v1/usage", json=event())
    r = client.post("/v1/usage", json=event(**change))
    assert r.status_code == 409
    assert count_events(conn) == 1
    row = conn.execute("SELECT model, tokens_in, tokens_out FROM usage_events").fetchone()
    assert (row["model"], row["tokens_in"], row["tokens_out"]) == ("model-a", 10, 5)


def test_same_key_for_two_tenants_is_two_events(client, conn):
    assert client.post("/v1/usage", json=event()).status_code == 201
    app.dependency_overrides[require_entitlement] = lambda: "tenant-2"
    assert client.post("/v1/usage", json=event(tenant_id="tenant-2")).status_code == 201
    assert count_events(conn) == 2


def test_tenant_mismatch_is_403_and_stores_nothing(client, conn):
    r = client.post("/v1/usage", json=event(tenant_id="tenant-2"))
    assert r.status_code == 403
    assert count_events(conn) == 0


@pytest.mark.parametrize("change", [
    {"occurred_at": "2026-11-10T10:30:00"},  # naive timestamp
    {"tokens_in": -1},
    {"tokens_out": -1},
    {"tokens_in": 1.5},
    {"tokens_in": 10**12 + 1},
    {"tokens_out": 10**12 + 1},
    {"tenant_id": ""},
    {"idempotency_key": ""},
    {"idempotency_key": "k" * 201},
    {"model": ""},
])
def test_invalid_body_is_422_and_stores_nothing(client, conn, change):
    assert client.post("/v1/usage", json=event(**change)).status_code == 422
    assert count_events(conn) == 0


def test_the_largest_token_counts_are_accepted(client):
    assert client.post("/v1/usage", json=event(tokens_in=10**12, tokens_out=10**12)).status_code == 201


def test_key_of_200_chars_is_accepted(client):
    assert client.post("/v1/usage", json=event(idempotency_key="k" * 200)).status_code == 201


def close_hour(conn, hour_start):
    conn.execute("INSERT INTO closed_hours (hour_start) VALUES (%s)", (hour_start,))
    conn.commit()


@pytest.mark.parametrize("occurred_at, late", [
    ("2026-11-10T10:00:00Z", True),            # first instant of the closed hour
    ("2026-11-10T10:59:59.999999Z", True),     # last instant of the closed hour
    ("2026-11-10T11:00:00Z", False),           # next hour
    ("2026-11-10T09:59:59.999999Z", False),    # previous hour
    ("2026-11-10T15:45:00+05:30", True),       # 10:15 UTC: the hour is the UTC hour, not the local one
])
def test_event_in_a_closed_hour_is_stored_late(client, conn, occurred_at, late):
    close_hour(conn, "2026-11-10T10:00:00Z")
    r = client.post("/v1/usage", json=event(occurred_at=occurred_at))
    assert r.status_code == 201
    row = conn.execute("SELECT late FROM usage_events WHERE id = %s", (r.json()["event_id"],)).fetchone()
    assert row["late"] is late
    assert r.json()["late"] is late  # the response says so too


def test_a_late_event_says_so_in_the_response_on_a_resend_and_in_the_log(client, conn, caplog):
    close_hour(conn, "2026-11-10T10:00:00Z")
    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        first = client.post("/v1/usage", json=event())
        again = client.post("/v1/usage", json=event())
    assert (first.status_code, first.json()["late"]) == (201, True)
    assert (again.status_code, again.json()["status"], again.json()["late"]) == (200, "duplicate", True)
    assert "usage accepted" in caplog.text and "late=True" in caplog.text
    assert "usage duplicate" in caplog.text


def test_late_check_uses_the_utc_hour_in_any_session_time_zone(client, conn, monkeypatch):
    # Asia/Kolkata is UTC+5:30: a local hour truncation would put 10:15 UTC in the 09:30 UTC hour.
    monkeypatch.setenv("PGOPTIONS", "-c timezone=Asia/Kolkata")
    close_hour(conn, "2026-11-10T10:00:00Z")
    r = client.post("/v1/usage", json=event(occurred_at="2026-11-10T10:15:00Z"))
    assert r.status_code == 201
    assert conn.execute("SELECT late FROM usage_events").fetchone()["late"] is True


def test_concurrent_double_send_is_counted_once(client, conn):
    senders = 8
    for key in ("race-1", "race-2", "race-3"):
        start = threading.Barrier(senders)

        def send():
            start.wait()  # release every sender at the same moment
            return TestClient(app).post("/v1/usage", json=event(idempotency_key=key)).status_code

        with ThreadPoolExecutor(max_workers=senders) as pool:
            codes = [f.result() for f in [pool.submit(send) for _ in range(senders)]]
        assert sorted(codes) == [200] * (senders - 1) + [201], key
        n = conn.execute("SELECT count(*) AS n FROM usage_events WHERE idempotency_key = %s", (key,)).fetchone()["n"]
        assert n == 1, key
