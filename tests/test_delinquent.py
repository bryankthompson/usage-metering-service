import json
from datetime import datetime, timedelta, timezone

import pytest

from app import delinquent, edge

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
H = timedelta(hours=1)


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    monkeypatch.setattr(edge, "now", lambda: NOW)


def put(conn, tenant, status, grace_until=None, failed_at=None):
    conn.execute("INSERT INTO entitlements (tenant_id, status, payment_failed_at, grace_until) VALUES (%s, %s, %s, %s)",
                 (tenant, status, failed_at, grace_until))
    conn.commit()


def run(capsys, *argv):
    code = delinquent.main(list(argv))
    return code, capsys.readouterr().out


def states(capsys, *argv):
    _, out = run(capsys, "--json", *argv)
    return [(r["tenant_id"], r["state"]) for r in map(json.loads, out.splitlines())]


def test_each_state_with_whole_hours(conn, capsys):
    put(conn, "a-grace", "past_due", NOW + 5 * H + timedelta(minutes=59), NOW - H)
    put(conn, "b-expired", "past_due", NOW - 2 * H - timedelta(minutes=1))
    put(conn, "c-none", "past_due")
    put(conn, "d-canceled", "canceled")
    put(conn, "e-active", "active")
    _, out = run(capsys, "--json")
    rows = {r["tenant_id"]: r for r in map(json.loads, out.splitlines())}
    assert set(rows) == {"a-grace", "b-expired", "c-none", "d-canceled"}
    assert (rows["a-grace"]["state"], rows["a-grace"]["hours_left"], rows["a-grace"]["hours_ago"]) == ("in_grace", 5, None)
    assert (rows["b-expired"]["state"], rows["b-expired"]["hours_ago"], rows["b-expired"]["hours_left"]) == ("grace_expired", 2, None)
    assert rows["c-none"]["state"] == "past_due_no_grace"
    assert rows["d-canceled"]["state"] == "canceled"
    assert rows["a-grace"]["payment_failed_at"] == (NOW - H).isoformat()


def test_boundary_now_equal_grace_until_is_still_in_grace(conn, capsys):
    put(conn, "edge", "past_due", NOW)
    assert states(capsys) == [("edge", "in_grace")]
    assert states(capsys, "--at", (NOW + timedelta(seconds=1)).isoformat()) == [("edge", "grace_expired")]


def test_order_denied_first_then_in_grace_by_least_time_left(conn, capsys):
    put(conn, "t1", "past_due", NOW + 10 * H)
    put(conn, "t2", "past_due", NOW + 2 * H)
    put(conn, "t3", "canceled")
    put(conn, "t4", "past_due")
    put(conn, "t5", "past_due", NOW - H)
    assert states(capsys) == [("t5", "grace_expired"), ("t4", "past_due_no_grace"), ("t3", "canceled"),
                              ("t2", "in_grace"), ("t1", "in_grace")]


def test_at_evaluates_at_that_instant(conn, capsys):
    put(conn, "t", "past_due", NOW + 48 * H)
    assert states(capsys) == [("t", "in_grace")]
    assert states(capsys, "--at", (NOW + 72 * H).isoformat()) == [("t", "grace_expired")]


def test_table_output(conn, capsys):
    put(conn, "t", "past_due", NOW + 3 * H)
    code, out = run(capsys)
    assert code == 0 and "in_grace" in out and "3h left" in out and not out.lstrip().startswith("{")


def test_fail_on_expired_exit_codes(conn, capsys):
    put(conn, "ok", "past_due", NOW + H)
    put(conn, "gone", "canceled")
    assert run(capsys, "--fail-on-expired")[0] == 0  # in grace and canceled do not trip it
    put(conn, "none", "past_due")
    assert run(capsys, "--fail-on-expired")[0] == 1
    conn.execute("UPDATE entitlements SET grace_until = %s WHERE tenant_id = 'none'", (NOW - H,))
    conn.commit()
    assert run(capsys, "--fail-on-expired")[0] == 1
    assert run(capsys)[0] == 0  # without the flag it only reports


def test_no_delinquent_tenants_is_clean(conn, capsys):
    put(conn, "fine", "active")
    code, out = run(capsys, "--fail-on-expired")
    assert (code, out.strip()) == (0, "no delinquent tenants")
    assert run(capsys, "--json") == (0, "")


@pytest.mark.parametrize("argv", [["--bogus"], ["--at", "yesterday"], ["--at", "2026-11-10T12:00:00"]])
def test_bad_argument_exits_2(conn, capsys, argv):
    with pytest.raises(SystemExit) as exc:
        delinquent.main(argv)
    assert exc.value.code == 2


def test_state_rule_matches_the_real_edge(conn):
    """The report's in_grace is exactly what the edge lets through, around the boundary."""
    from fastapi import Depends, FastAPI
    from fastapi.testclient import TestClient
    probe = FastAPI()

    @probe.get("/probe")
    def probe_route(tenant: str = Depends(edge.require_entitlement)) -> dict[str, str]:
        return {"tenant": tenant}

    client = TestClient(probe)
    for i, grace in enumerate((None, NOW - H, NOW - timedelta(seconds=1), NOW, NOW + timedelta(seconds=1))):
        put(conn, f"t{i}", "past_due", grace)
        allowed = client.get("/probe", headers={"X-Tenant-Id": f"t{i}"}).status_code == 200
        assert (delinquent.classify("past_due", grace, NOW)[0] == "in_grace") == allowed, grace
