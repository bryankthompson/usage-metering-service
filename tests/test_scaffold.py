import psycopg
import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app


def test_healthz():
    assert TestClient(app).get("/healthz").json() == {"status": "ok"}


def test_schema_is_repeatable(conn):
    db.apply_schema(conn)
    db.apply_schema(conn)


def test_usage_events_is_append_only(conn):
    conn.execute(
        "INSERT INTO usage_events (tenant_id, idempotency_key, model, tokens_in, tokens_out, occurred_at)"
        " VALUES ('t', 'k', 'm', 1, 2, now())")
    conn.commit()
    with pytest.raises(psycopg.errors.RaiseException):
        conn.execute("UPDATE usage_events SET tokens_in = 99")
    conn.rollback()
    with pytest.raises(psycopg.errors.RaiseException):
        conn.execute("DELETE FROM usage_events")


def test_edge_fails_closed_until_implemented():
    # The stub denies everything: a request without a real entitlement check never gets through.
    from app.edge import require_entitlement
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as err:
        require_entitlement()
    assert err.value.status_code == 503
