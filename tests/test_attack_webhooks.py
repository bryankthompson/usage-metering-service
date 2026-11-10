"""Adversarial tests for POST /v1/stripe/webhook: forged and odd signatures, signed but hostile payload shapes,
concurrent redelivery, and the grace window edges. Nothing here talks to Stripe; the secret is the fake one.

The rule these tests enforce: a request that is not signed is 400 before the database is touched, and a signed event
with an unusable shape is never a 500 (Stripe would retry a 500 for days and the event could never succeed).
"""
import json
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
import stripe
from fastapi.testclient import TestClient

from app.main import app
from tests.stripe_signing import SECRET, event, sign, subscription_event

URL = "/v1/stripe/webhook"
NOW = 1_793_000_000                                  # a fixed "now" in unix seconds for the tolerance tests


@pytest.fixture(autouse=True)
def webhook_env(monkeypatch):
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", SECRET)
    monkeypatch.delenv("GRACE_PERIOD_HOURS", raising=False)


@pytest.fixture
def client():
    # A 500 must show up as a status code the test can assert on, not as an exception inside the client.
    return TestClient(app, raise_server_exceptions=False)


def post(client, payload: bytes, signature: str | None = None):
    headers = {"Stripe-Signature": signature if signature is not None else sign(payload)}
    return client.post(URL, content=payload, headers=headers)


def counts(conn):
    return (conn.execute("SELECT count(*) AS n FROM entitlements").fetchone()["n"],
            conn.execute("SELECT count(*) AS n FROM stripe_events").fetchone()["n"])


def fresh_event(event_id="evt_1", **kwargs) -> bytes:
    return subscription_event(event_id, int(time.time()), **kwargs)


# --- signatures: every bad one is a 400 and changes nothing ---

def test_a_signature_made_with_another_secret_is_400(client, conn):
    payload = fresh_event()
    assert post(client, payload, sign(payload, secret="whsec_test_some_other_secret")).status_code == 400
    assert counts(conn) == (0, 0)


def test_a_single_flipped_byte_in_the_body_is_400(client, conn):
    payload = fresh_event()
    flipped = payload.replace(b"tenant-1", b"tenant-2")
    assert len(flipped) == len(payload)
    assert post(client, flipped, sign(payload)).status_code == 400
    assert counts(conn) == (0, 0)


def test_the_signature_must_match_the_timestamp_it_names(client, conn):
    payload = fresh_event()
    header = sign(payload, timestamp=int(time.time()) - 10)
    moved = header.replace("t=", "t=1", 1)                      # same digest, a different timestamp
    assert post(client, payload, moved).status_code == 400
    assert counts(conn) == (0, 0)


@pytest.mark.parametrize("order", ["bad_then_good", "good_then_bad"])
def test_any_one_matching_v1_among_several_is_accepted(client, conn, order):
    payload = fresh_event()
    good = sign(payload).split(",v1=")[1]
    t = sign(payload).split(",")[0]
    pair = ["v1=" + "0" * 64, "v1=" + good]
    header = t + "," + ",".join(pair if order == "bad_then_good" else reversed(pair))
    assert post(client, payload, header).status_code == 200


def test_several_v1_values_that_all_mismatch_are_400(client, conn):
    payload = fresh_event()
    header = sign(payload).split(",")[0] + ",v1=" + "0" * 64 + ",v1=" + "1" * 64
    assert post(client, payload, header).status_code == 400
    assert counts(conn) == (0, 0)


@pytest.mark.parametrize("header", ["", " ", "t=", "v1=abc", "t=abc,v1=abc", "t=1,v0=abc", "garbage", "t=1,v1=", ",,,", "t=99999999999999999999999,v1=00"])
def test_empty_or_garbled_signature_headers_are_400(client, conn, header):
    resp = client.post(URL, content=fresh_event(), headers={"Stripe-Signature": header})
    assert resp.status_code == 400
    assert counts(conn) == (0, 0)


def test_no_signature_header_at_all_is_400(client, conn):
    assert client.post(URL, content=fresh_event()).status_code == 400
    assert counts(conn) == (0, 0)


@pytest.mark.parametrize("age, status", [(0, 200), (299, 200), (300, 200), (301, 400), (3600, 400)])
def test_the_five_minute_tolerance_is_inclusive_at_exactly_300_seconds(client, conn, monkeypatch, age, status):
    monkeypatch.setattr(stripe._webhook, "time", SimpleNamespace(time=lambda: NOW))     # freeze the library's clock
    payload = subscription_event("evt_age", NOW, tenant_id="tenant-1")
    assert post(client, payload, sign(payload, timestamp=NOW - age)).status_code == status


def test_a_signature_dated_in_the_future_is_accepted_because_only_old_ones_expire(client, conn, monkeypatch):
    # Pins today's behaviour of the library: a future timestamp never expires, but only the holder of the secret can
    # produce one, so it is not a replay hole. If this starts failing, the library changed its rule.
    monkeypatch.setattr(stripe._webhook, "time", SimpleNamespace(time=lambda: NOW))
    payload = subscription_event("evt_future", NOW, tenant_id="tenant-1")
    assert post(client, payload, sign(payload, timestamp=NOW + 3600)).status_code == 200


def test_a_body_that_is_not_utf8_is_400_signed_or_not(client, conn):
    payload = b'{"id": "evt_\xff\xfe", "type": "x"}'
    assert post(client, payload).status_code == 400
    assert client.post(URL, content=payload, headers={"Stripe-Signature": "t=1,v1=00"}).status_code == 400
    assert counts(conn) == (0, 0)


@pytest.mark.parametrize("secret", ["", " ", "\t\n"])
def test_a_blank_secret_setting_is_503_not_a_bad_signature(client, conn, monkeypatch, secret):
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", secret)
    assert post(client, fresh_event()).status_code == 503
    assert counts(conn) == (0, 0)


def test_an_empty_body_is_400(client, conn):
    assert post(client, b"").status_code == 400
    assert counts(conn) == (0, 0)


def test_a_body_over_one_mebibyte_is_413_before_anything_else_and_exactly_one_mebibyte_is_still_read(client, conn):
    # The body is read before the signature can be checked, so it is capped: 600 MB unsigned took the API to 2.4 GiB.
    limit = 1024 * 1024
    started = time.monotonic()
    assert client.post(URL, content=b"x" * (limit + 1), headers={"Stripe-Signature": "t=1,v1=00"}).status_code == 413
    assert client.post(URL, content=b"x" * (20 * 1024 * 1024), headers={"Stripe-Signature": "t=1,v1=00"}).status_code == 413
    assert time.monotonic() - started < 5
    assert client.post(URL, content=b"x" * limit, headers={"Stripe-Signature": "t=1,v1=00"}).status_code == 400   # read, then refused
    assert counts(conn) == (0, 0)


def test_a_chunked_body_over_the_cap_is_413_too(client, conn):
    def chunks():
        for _ in range(3):
            yield b"x" * (512 * 1024)                          # no Content-Length: the size is only known by counting

    resp = client.post(URL, content=chunks(), headers={"Stripe-Signature": "t=1,v1=00"})
    assert resp.status_code == 413
    assert counts(conn) == (0, 0)


# --- signed, valid JSON, wrong shape: 400 or ignored, never a 500 ---

@pytest.mark.parametrize("payload", [b"[]", b"null", b"123", b'"text"', b"true", b"{}", b'{"id": "e"}', b'{"id": "e", "type": "customer.subscription.updated"}',
                                     b'{"id": "e", "type": "customer.subscription.updated", "created": 1}',
                                     b'{"id": "e", "type": "customer.subscription.updated", "created": 1, "data": null}',
                                     b'{"id": "e", "type": "customer.subscription.updated", "created": 1, "data": {"object": []}}',
                                     b'{"id": "e", "type": "customer.subscription.updated", "created": "soon", "data": {"object": {}}}',
                                     b'{"id": "e", "type": "customer.subscription.updated", "created": 1e999, "data": {"object": {}}}',
                                     b'{"id": "e", "type": ["a"], "created": 1, "data": {"object": {}}}',
                                     b'{"id": ["e"], "type": "customer.subscription.updated", "created": 1, "data": {"object": {}}}'])
def test_signed_json_of_the_wrong_shape_is_400_and_changes_nothing(client, conn, payload):
    assert post(client, payload).status_code == 400
    assert counts(conn) == (0, 0)


def test_signed_json_nested_100000_deep_is_400_not_a_crash(client, conn):
    assert post(client, b"[" * 100_000 + b"]" * 100_000).status_code == 400
    assert counts(conn) == (0, 0)


def sub_event_with(event_id: str, **sub_fields) -> bytes:
    sub = {"id": "sub_1", "object": "subscription", "customer": "cus_1", "status": "active",
           "metadata": {"tenant_id": "tenant-1"}, "items": {"data": [{"price": {"id": "price_1"}}]}}
    sub.update(sub_fields)
    return event(event_id, "customer.subscription.updated", int(time.time()), sub)


@pytest.mark.parametrize("tenant_id", [
    123, True, ["tenant-1"], {"a": 1}, "", "a\u0000b", None,
    " t", "t ", "\tt", "t\t", "  ", "a\nb", "a\rb", "a\x07b", "a\x7fb", "a\tb",       # what the edge could never match, and control characters
    "x" * 201,                                                                       # over the 200 characters /v1/usage accepts
    "\ud800", "a\ud800b"])                                                          # a lone surrogate cannot be stored
def test_a_subscription_whose_tenant_id_is_not_usable_text_is_ignored(client, conn, tenant_id):
    resp = post(client, sub_event_with("evt_t", metadata={"tenant_id": tenant_id}))
    assert (resp.status_code, resp.json()) == (200, {"status": "ignored"})
    assert counts(conn) == (0, 0)


@pytest.mark.parametrize("metadata", [[], "text", 5, True, [{"tenant_id": "tenant-1"}]])
def test_a_subscription_whose_metadata_is_not_an_object_is_ignored(client, conn, metadata):
    resp = post(client, sub_event_with("evt_m", metadata=metadata))
    assert (resp.status_code, resp.json()) == (200, {"status": "ignored"})
    assert counts(conn) == (0, 0)


@pytest.mark.parametrize("tenant_id", ["x" * 200, "テナント-é-😀", "a b", "ａｂｃ", "\u00a0nbsp\u00a0", "\u200bzwsp", "x'; DROP TABLE entitlements; --", "Robert'); DELETE FROM usage_events;--"])
def test_odd_but_valid_tenant_ids_are_stored_exactly_and_the_tables_survive(client, conn, tenant_id):
    resp = post(client, sub_event_with("evt_odd", metadata={"tenant_id": tenant_id}))
    assert (resp.status_code, resp.json()) == (200, {"status": "processed"})
    stored = conn.execute("SELECT tenant_id FROM entitlements").fetchall()
    assert [r["tenant_id"] for r in stored] == [tenant_id]
    assert conn.execute("SELECT count(*) AS n FROM stripe_events").fetchone()["n"] == 1


@pytest.mark.parametrize("fields", [
    {"items": {"data": "x"}}, {"items": {"data": [1]}}, {"items": {"data": [{"price": "p"}]}}, {"items": []}, {"items": "x"},
    {"items": {"data": [{"price": {"id": 5}}]}}, {"items": {"data": [{"price": {"id": "p\u0000"}}]}},
    {"status": ["active"]}, {"status": {"a": 1}}, {"status": None}, {"status": 5},
    {"customer": 123}, {"customer": {"a": 1}}, {"customer": "c\u0000x"}, {"customer": None},
    {"id": 5}, {"id": "s\u0000"}, {"id": None},
    {"customer": "\ud800"}, {"id": "s\ud800"}, {"items": {"data": [{"price": {"id": "p\ud800"}}]}}])
def test_odd_subscription_fields_never_make_a_500(client, conn, fields):
    resp = post(client, sub_event_with("evt_f", **fields))
    assert resp.status_code == 200, resp.text
    row = conn.execute("SELECT status FROM entitlements WHERE tenant_id = 'tenant-1'").fetchone()
    assert row is not None
    if "status" in fields:
        assert row["status"] == "canceled"                    # an unreadable status is never access


@pytest.mark.parametrize("customer", [123, {"a": 1}, ["cus_1"], "c\u0000x", None, "", True, "\ud800", "c\ud800"])
def test_payment_failed_with_an_unusable_customer_is_ignored(client, conn, customer):
    payload = event("evt_pf", "invoice.payment_failed", int(time.time()), {"id": "in_1", "customer": customer})
    resp = post(client, payload)
    assert (resp.status_code, resp.json()) == (200, {"status": "ignored"})
    assert counts(conn) == (0, 0)


@pytest.mark.parametrize("event_id", ["evt_\u0000", "evt_\ud800"])
def test_an_event_id_that_cannot_be_stored_is_400(client, conn, event_id):
    payload = event(event_id, "customer.subscription.updated", int(time.time()), {"metadata": {"tenant_id": "t"}})
    assert post(client, payload).status_code == 400
    assert counts(conn) == (0, 0)


# --- the same event, delivered many times at once ---

def test_one_event_delivered_by_ten_threads_is_processed_once_and_stored_once(conn):
    payload = fresh_event("evt_race")

    def deliver(_):
        return TestClient(app, raise_server_exceptions=False).post(URL, content=payload, headers={"Stripe-Signature": sign(payload)})

    with ThreadPoolExecutor(max_workers=10) as pool:
        responses = list(pool.map(deliver, range(10)))
    assert [r.status_code for r in responses] == [200] * 10
    outcomes = sorted(r.json()["status"] for r in responses)
    assert outcomes == ["duplicate"] * 9 + ["processed"]
    assert counts(conn) == (1, 1)


def test_different_events_for_one_tenant_at_once_leave_a_consistent_row(conn):
    # Twenty events for a brand-new tenant, delivered together, with distinct ids and ascending times.
    base = int(time.time())
    payloads = [subscription_event(f"evt_{i}", base + i, tenant_id="tenant-new", status="active" if i % 2 else "past_due") for i in range(20)]

    def deliver(payload):
        return TestClient(app, raise_server_exceptions=False).post(URL, content=payload, headers={"Stripe-Signature": sign(payload)})

    with ThreadPoolExecutor(max_workers=10) as pool:
        responses = list(pool.map(deliver, payloads))
    assert {r.status_code for r in responses} == {200}
    assert {r.json()["status"] for r in responses} <= {"processed", "stale"}
    row = conn.execute("SELECT status, last_event_at FROM entitlements WHERE tenant_id = 'tenant-new'").fetchone()
    assert conn.execute("SELECT count(*) AS n FROM entitlements").fetchone()["n"] == 1
    assert int(row["last_event_at"].timestamp()) == base + 19         # the newest event won, whatever the arrival order
    assert row["status"] == "active"


# --- grace window edges ---

def test_grace_period_of_zero_hours_denies_access_straight_away(client, conn, monkeypatch):
    monkeypatch.setenv("GRACE_PERIOD_HOURS", "0")
    post(client, subscription_event("evt_s", int(time.time()) - 100, customer="cus_z"))
    created = int(time.time()) - 5
    payload = event("evt_z", "invoice.payment_failed", created, {"id": "in_z", "customer": "cus_z"})
    assert post(client, payload).json() == {"status": "processed"}
    row = conn.execute("SELECT status, payment_failed_at, grace_until FROM entitlements").fetchone()
    assert row["status"] == "past_due" and row["grace_until"] == row["payment_failed_at"]
    from app import edge
    assert edge.now() > row["grace_until"]                                   # so the edge's `now <= grace_until` is false


@pytest.mark.parametrize("value", ["8760", "0"])
def test_grace_period_limits_are_accepted(client, conn, monkeypatch, value):
    monkeypatch.setenv("GRACE_PERIOD_HOURS", value)
    post(client, fresh_event("evt_s", customer="cus_l"))
    payload = event("evt_l", "invoice.payment_failed", int(time.time()), {"id": "in_l", "customer": "cus_l"})
    assert post(client, payload).status_code == 200


@pytest.mark.parametrize("value", ["-1", "8761", "", " ", "3.5", "1e3", "abc", "999999999999999999999", "NaN"])
def test_grace_period_outside_its_range_is_503_and_nothing_is_recorded(client, conn, monkeypatch, value):
    monkeypatch.setenv("GRACE_PERIOD_HOURS", value)
    post_ok = post(client, fresh_event("evt_s", customer="cus_b"))
    assert post_ok.status_code == 200                                         # a subscription event does not need the setting
    events_before = counts(conn)
    payload = event("evt_b", "invoice.payment_failed", int(time.time()), {"id": "in_b", "customer": "cus_b"})
    assert post(client, payload).status_code == 503
    assert counts(conn) == events_before


# --- a cancel must not leave a live grace window behind ---

def test_a_canceled_tenant_does_not_come_back_through_an_old_grace_window(client, conn):
    # Failure -> grace window; the subscription is then deleted; a later past_due event must not revive the old window.
    base = int(time.time())
    post(client, subscription_event("evt_1", base - 30, tenant_id="tenant-1", customer="cus_1"))
    post(client, event("evt_2", "invoice.payment_failed", base - 20, {"id": "in_1", "customer": "cus_1"}))
    assert conn.execute("SELECT grace_until FROM entitlements").fetchone()["grace_until"] is not None
    post(client, subscription_event("evt_3", base - 10, tenant_id="tenant-1", customer="cus_1", event_type="customer.subscription.deleted"))
    row = conn.execute("SELECT status, grace_until FROM entitlements").fetchone()
    assert row["status"] == "canceled" and row["grace_until"] is None
    post(client, subscription_event("evt_4", base, tenant_id="tenant-1", customer="cus_1", status="past_due"))
    row = conn.execute("SELECT status, grace_until FROM entitlements").fetchone()
    assert row["status"] == "past_due" and row["grace_until"] is None          # past_due with no window is denied by the edge


def test_livemode_is_not_read_so_a_live_mode_event_signed_with_the_secret_is_applied(client, conn):
    # Pins what the README says under "Not built": test mode is the fake secret, not a check in the code.
    sub = {"id": "sub_1", "customer": "cus_1", "status": "active", "metadata": {"tenant_id": "tenant-1"}}
    payload = json.dumps({"id": "evt_live", "type": "customer.subscription.updated", "created": int(time.time()), "livemode": True,
                          "data": {"object": sub}}).encode()
    assert post(client, payload).json() == {"status": "processed"}


@pytest.mark.parametrize("header", [b"t=1,v1=\xff\xfe", b"\xff", b"t=\xe3\x83\x86,v1=00", b"t=1,v1=\x00\x01", "t=" + "9" * 5000 + ",v1=00"])
def test_signature_headers_with_odd_bytes_are_400_never_a_500(client, conn, header):
    resp = client.post(URL, content=fresh_event(), headers={"Stripe-Signature": header})
    assert resp.status_code == 400
    assert counts(conn) == (0, 0)


# --- a tenant id or event id with a newline must not forge a log line ---

def test_odd_ids_are_logged_as_repr_so_one_event_is_one_log_line(client, conn, caplog):
    caplog.set_level("INFO")
    post(client, subscription_event("evt_a\nFORGED webhook processed: tenant=admin", int(time.time()), tenant_id="tenant-1"))
    post(client, subscription_event("evt_b", int(time.time()), tenant_id="bad\nFORGED edge denied: tenant=admin"))
    post(client, event("evt_c\nFORGED", "invoice.unknown\nFORGED", int(time.time()), {}))
    lines = [r.getMessage() for r in caplog.records if r.name.startswith("app.")]
    assert lines and not [m for m in lines if "\n" in m], [m for m in lines if "\n" in m]
    assert any("tenant_id" in m and "is not usable" in m for m in lines)          # the refusal is logged, not silent


def test_a_tenant_id_at_the_limit_is_stored_and_one_over_is_ignored_and_logged(client, conn, caplog):
    caplog.set_level("INFO")
    assert post(client, sub_event_with("evt_200", metadata={"tenant_id": "t" * 200})).json() == {"status": "processed"}
    assert post(client, sub_event_with("evt_201", metadata={"tenant_id": "t" * 201})).json() == {"status": "ignored"}
    assert [r["tenant_id"] for r in conn.execute("SELECT tenant_id FROM entitlements").fetchall()] == ["t" * 200]
    assert "is not usable" in caplog.text
