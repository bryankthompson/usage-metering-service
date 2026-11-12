from datetime import datetime, timedelta, timezone

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app import edge
from app.edge import require_entitlement

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
HEADERS = {"X-Tenant-Id": "tenant-1"}


@pytest.fixture
def client(monkeypatch):
    """A one-route app behind the real edge, with the clock fixed at NOW (no sleeping)."""
    monkeypatch.setattr(edge, "now", lambda: NOW)
    probe = FastAPI()

    @probe.get("/probe")
    def probe_route(tenant: str = Depends(require_entitlement)) -> dict[str, str]:
        return {"tenant": tenant}

    return TestClient(probe)


def entitle(conn, status, grace_until=None, tenant="tenant-1"):
    conn.execute(
        "INSERT INTO entitlements (tenant_id, status, grace_until) VALUES (%s, %s, %s)", (tenant, status, grace_until)
    )
    conn.commit()


def test_missing_header_is_401(client, conn):
    entitle(conn, "active")
    assert client.get("/probe").status_code == 401
    assert client.get("/probe", headers={"X-Tenant-Id": "  "}).status_code == 401


def test_no_entitlement_row_is_402(client, conn):
    assert client.get("/probe", headers=HEADERS).status_code == 402


def test_canceled_is_402(client, conn):
    entitle(conn, "canceled")
    assert client.get("/probe", headers=HEADERS).status_code == 402


def test_active_is_allowed_and_returns_the_tenant(client, conn):
    entitle(conn, "active")
    resp = client.get("/probe", headers=HEADERS)
    assert resp.status_code == 200 and resp.json() == {"tenant": "tenant-1"}


def test_active_tenant_is_not_another_tenant(client, conn):
    entitle(conn, "active")
    assert client.get("/probe", headers={"X-Tenant-Id": "tenant-2"}).status_code == 402


def test_past_due_is_allowed_inside_the_grace_window(client, conn):
    entitle(conn, "past_due", grace_until=NOW + timedelta(seconds=1))
    assert client.get("/probe", headers=HEADERS).status_code == 200


def test_past_due_is_allowed_exactly_at_the_end_of_the_grace_window(client, conn):
    entitle(conn, "past_due", grace_until=NOW)
    assert client.get("/probe", headers=HEADERS).status_code == 200


def test_past_due_is_402_after_the_grace_window(client, conn):
    entitle(conn, "past_due", grace_until=NOW - timedelta(seconds=1))
    assert client.get("/probe", headers=HEADERS).status_code == 402


def test_past_due_with_no_grace_window_is_402(client, conn):
    entitle(conn, "past_due", grace_until=None)
    assert client.get("/probe", headers=HEADERS).status_code == 402


def test_unknown_status_is_402(client, monkeypatch):
    # The schema refuses such a row (tests/test_entitlements_status_check.py), so the row the edge reads is stubbed.
    class Stub:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, *args):
            return self

        def fetchone(self):
            return {"status": "something_new", "grace_until": None}

    monkeypatch.setattr(edge, "request_connection", Stub)
    resp = client.get("/probe", headers=HEADERS)
    assert (resp.status_code, resp.json()["detail"]) == (
        402,
        "not entitled: status something_new",
    )  # not "no entitlement": the stub was read


def test_database_down_is_503_never_allowed(client, conn, monkeypatch):
    entitle(conn, "active")  # would be allowed if the check could run
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql://metering:metering@localhost:1/metering"
    )  # nothing listens on port 1
    resp = client.get("/probe", headers=HEADERS)
    assert resp.status_code == 503
