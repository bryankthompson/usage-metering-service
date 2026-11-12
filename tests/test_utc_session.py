"""Every connection the app opens pins its session time zone to UTC, so a PGTZ, a URL option or the server's own setting
cannot change what the driver hands back (RT claims lens: under America/New_York, reconcile printed the local clock hour
with a literal Z, charges/409/delinquent printed `-05:00`, and a year-one date was a 500)."""

import json
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from app import db, delinquent, edge, reconcile
from app.edge import require_entitlement
from app.main import app
from app.pricing import add_price_version
from app.rollup import close_hour
from tests.test_attack_ingest import event
from tests.test_reconcile import H, add_event

HOUR_Z = "2026-11-10T10:00:00Z"
YEAR_ONE = datetime(1, 1, 1, tzinfo=timezone.utc)
RANGE = {"start": "2026-11-10T00:00:00Z", "end": "2026-11-11T00:00:00Z"}


@pytest.fixture
def new_york(monkeypatch):
    """Everything connecting from here on is asked for America/New_York by the environment."""
    monkeypatch.setenv("PGTZ", "America/New_York")


@pytest.fixture
def client(conn):
    app.dependency_overrides[require_entitlement] = lambda: "t1"
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()


def show(connection, setting):
    return connection.execute(f"SHOW {setting}").fetchone()[setting]


def with_option(option: str) -> str:
    base = db.database_url()
    return base + ("&" if "?" in base else "?") + "options=" + quote(option)


def test_a_connection_reports_utc_whatever_the_environment_says(new_york):
    connection = db.connect()
    try:
        assert show(connection, "TimeZone") == "UTC"
        assert show(connection, "statement_timeout") == "15s"  # the setting it shares a statement with is still set
    finally:
        connection.close()


def test_a_url_option_cannot_change_the_zone_either():
    connection = db.connect(with_option("-c TimeZone=Asia/Kolkata"))
    try:
        assert show(connection, "TimeZone") == "UTC"
    finally:
        connection.close()


def test_the_jobs_that_run_without_a_statement_timeout_are_pinned_too(new_york):
    connection = db.connect(statement_timeout_ms=0)
    try:
        assert (show(connection, "TimeZone"), show(connection, "statement_timeout")) == ("UTC", "0")
    finally:
        connection.close()


def test_a_datetime_comes_back_as_utc(new_york):
    connection = db.connect()
    try:
        value = connection.execute("SELECT '2026-11-10T10:00:00Z'::timestamptz AS t").fetchone()["t"]
        assert value.utcoffset() == timedelta(0) and value.hour == 10
    finally:
        connection.close()


def test_reconcile_names_the_utc_hour_of_a_drift_line(conn, new_york, capsys):
    add_event(conn, "a")
    close_hour(conn, H)
    conn.execute("UPDATE usage_rollups_hourly SET tokens_in = tokens_in + 1")
    conn.commit()
    assert reconcile.main([]) == 1
    out = capsys.readouterr().out
    assert f"hour={HOUR_Z} tokens_in" in out and "T05:00:00Z" not in out


def test_charges_hour_start_is_z_not_the_session_offset(client, conn, new_york):
    add_price_version(conn, "m", datetime(2020, 1, 1, tzinfo=timezone.utc), 1000, 1000)
    conn.execute("INSERT INTO closed_hours (hour_start) VALUES (%s)", (H,))
    conn.execute(
        "INSERT INTO usage_rollups_hourly (tenant_id, model, hour_start, tokens_in, tokens_out, event_count)"
        " VALUES ('t1', 'm', %s, 1000, 0, 1)",
        (H,),
    )
    conn.commit()
    r = client.get("/v1/tenants/t1/charges", params=RANGE)
    assert (r.status_code, [line["hour_start"] for line in r.json()["lines"]]) == (200, [HOUR_Z])


def test_the_409_text_names_the_hour_in_utc(client, conn, new_york):
    conn.execute("INSERT INTO closed_hours (hour_start) VALUES (%s)", (H,))
    conn.execute(
        "INSERT INTO usage_rollups_hourly (tenant_id, model, hour_start, tokens_in, tokens_out, event_count)"
        " VALUES ('t1', 'unpriced', %s, 1, 1, 1)",
        (H,),
    )
    conn.commit()
    r = client.get("/v1/tenants/t1/charges", params=RANGE)
    assert (r.status_code, r.json()["detail"]) == (409, "no price for model 'unpriced' at 2026-11-10T10:00:00+00:00")


def test_delinquent_json_gives_utc_offsets(conn, new_york, capsys, monkeypatch):
    now = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(edge, "now", lambda: now)
    conn.execute(
        "INSERT INTO entitlements (tenant_id, status, payment_failed_at, grace_until) VALUES ('a', 'past_due', %s, %s)",
        (now - timedelta(hours=1), now + timedelta(hours=5)),
    )
    conn.commit()
    delinquent.main(["--json"])
    row = json.loads(capsys.readouterr().out.splitlines()[0])
    assert (row["payment_failed_at"], row["grace_until"]) == ("2026-11-10T11:00:00+00:00", "2026-11-10T17:00:00+00:00")


def test_a_year_one_event_is_accepted_and_resent_not_a_500(client, conn, new_york):
    payload = event(occurred_at="0001-01-01T00:00:00Z")
    assert client.post("/v1/usage", json=payload).status_code == 201
    assert client.post("/v1/usage", json=payload).status_code == 200


def test_a_year_one_price_version_does_not_break_charges(client, conn, new_york):
    add_price_version(conn, "m", YEAR_ONE, 1000, 1000)
    conn.execute("INSERT INTO closed_hours (hour_start) VALUES (%s)", (H,))
    conn.execute(
        "INSERT INTO usage_rollups_hourly (tenant_id, model, hour_start, tokens_in, tokens_out, event_count)"
        " VALUES ('t1', 'm', %s, 1000, 0, 1)",
        (H,),
    )
    conn.commit()
    r = client.get("/v1/tenants/t1/charges", params=RANGE)
    assert (r.status_code, r.json()["total_micros"]) == (200, 1000)
