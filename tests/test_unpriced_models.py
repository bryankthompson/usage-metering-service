"""A charges range with an unpriced model is still a 409 (no partial bills), but the detail names EVERY unpriced model in
the range, with the first hour of each, so the operator can add all the missing prices in one pass (RT-money-3)."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.edge import require_entitlement
from app.main import app
from app.pricing import add_price_version

T0 = datetime(2026, 11, 1, 0, 0, tzinfo=timezone.utc)
RANGE = {"start": "2026-11-01T00:00:00+00:00", "end": "2026-11-01T03:00:00+00:00"}


def iso(hours: int) -> str:
    return (T0 + timedelta(hours=hours)).isoformat()


@pytest.fixture
def client():
    app.dependency_overrides[require_entitlement] = lambda: "tenant-1"
    yield TestClient(app)
    app.dependency_overrides.clear()


def usage(conn, model: str, *hours: int):
    for h in hours:
        conn.execute(
            "INSERT INTO usage_rollups_hourly (tenant_id, model, hour_start, tokens_in, tokens_out, event_count)"
            " VALUES ('tenant-1', %s, %s, 1000, 1000, 1)",
            (model, T0 + timedelta(hours=h)),
        )
    conn.commit()


def charges(client):
    return client.get("/v1/tenants/tenant-1/charges", params=RANGE)


def test_one_unpriced_model_keeps_the_message_it_always_had(client, conn):
    add_price_version(conn, "good", T0, 1000, 1000)
    usage(conn, "good", 0, 1)
    usage(conn, "gpt", 1, 2)
    r = charges(client)
    assert (r.status_code, r.json()) == (409, {"detail": f"no price for model 'gpt' at {iso(1)}"})


def test_every_unpriced_model_is_named_sorted_with_its_first_hour(client, conn):
    add_price_version(conn, "good", T0, 1000, 1000)
    add_price_version(conn, "priced-later", T0 + timedelta(hours=2), 1000, 1000)
    usage(conn, "good", 0, 1, 2)
    usage(conn, "b-model", 1, 2)  # never priced, first seen at hour 1
    usage(conn, "a-model", 0, 1, 2)  # never priced, first seen at hour 0
    usage(conn, "priced-later", 0, 1, 2)  # unpriced for hours 0 and 1, priced from hour 2
    r = charges(client)
    assert r.status_code == 409
    assert r.json() == {
        "detail": f"no price for 3 models: 'a-model' at {iso(0)}, 'b-model' at {iso(1)}, " f"'priced-later' at {iso(0)}"
    }


def test_a_priced_model_is_never_named(client, conn):
    add_price_version(conn, "good", T0, 1000, 1000)
    usage(conn, "good", 0, 1, 2)
    usage(conn, "other", 0)
    assert "good" not in charges(client).json()["detail"]


def test_model_names_are_compared_byte_for_byte(client, conn):
    add_price_version(conn, "GPT", T0, 1000, 1000)
    usage(conn, "GPT", 0)
    usage(conn, "gpt", 0)
    usage(conn, "GPT ", 0)
    detail = charges(client).json()["detail"]
    assert detail == f"no price for 2 models: 'GPT ' at {iso(0)}, 'gpt' at {iso(0)}"
    assert "'GPT' at" not in detail


def test_more_than_twenty_unpriced_models_are_cut_with_a_count(client, conn):
    for i in range(25):
        usage(conn, f"m{i:02d}", 0)
    detail = charges(client).json()["detail"]
    assert detail.startswith("no price for 25 models: 'm00' at ") and detail.endswith(", and 5 more")
    assert "'m19' at" in detail and "'m20'" not in detail and detail.count(" at ") == 20


def test_exactly_twenty_unpriced_models_are_all_named_without_a_tail(client, conn):
    for i in range(20):
        usage(conn, f"m{i:02d}", 0)
    detail = charges(client).json()["detail"]
    assert "'m19' at" in detail and "more" not in detail


def test_it_is_still_all_or_nothing_no_partial_bill(client, conn):
    add_price_version(conn, "good", T0, 1000, 1000)
    usage(conn, "good", 0, 1, 2)
    usage(conn, "bad", 2)
    r = charges(client)
    assert r.status_code == 409 and set(r.json()) == {"detail"}


def test_an_unpriced_model_outside_the_range_is_not_named_and_does_not_block(client, conn):
    add_price_version(conn, "good", T0, 1000, 1000)
    usage(conn, "good", 0)
    usage(conn, "bad", 5)  # hour 5 is past the range end (hour 3)
    r = charges(client)
    assert r.status_code == 200 and r.json()["total_micros"] == 2000


def test_a_model_name_with_a_newline_cannot_forge_a_log_line(client, conn, caplog):
    usage(conn, "x\nFORGED line", 0)
    charges(client)
    assert all("\n" not in record.getMessage() for record in caplog.records)
