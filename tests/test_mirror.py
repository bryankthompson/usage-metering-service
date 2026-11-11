"""The ClickHouse mirror against a stand-in HTTP server (no real ClickHouse): the request it builds, that a failing or
absent ClickHouse never fails the close, and that backfill sends only hours that differ. The real thing was run by hand
(docs/clickhouse.md)."""
import json
import logging
import socket
import threading
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from app import mirror
from app.rollup import close_hour
from tests.test_rollup import H, add_event, closed_hours, rollups


class StandIn:
    """Records every request; keeps inserted rows so a SELECT answers with them. `status` overrides every answer."""

    def __init__(self):
        self.requests = []  # (query, body)
        self.rows = {}
        self.status = 200
        standin = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
                query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)["query"][0]
                standin.requests.append((query, body))
                out = b""
                if standin.status == 200 and query.startswith("INSERT"):
                    for line in body.splitlines():
                        r = json.loads(line)
                        standin.rows[(r["tenant_id"], r["model"], r["hour_start"])] = r
                elif standin.status == 200 and query.startswith("SELECT"):
                    ts = lambda s: int(datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp())
                    out = "\n".join(json.dumps({**r, "ts": str(ts(r["hour_start"]))}) for r in standin.rows.values()).encode()
                self.send_response(standin.status)
                self.end_headers()
                self.wfile.write(out if standin.status == 200 else b"boom")

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def inserts(self):
        return [(q, b) for q, b in self.requests if q.startswith("INSERT")]


@pytest.fixture
def ch(monkeypatch):
    standin = StandIn()
    monkeypatch.setenv("CLICKHOUSE_URL", standin.url)
    yield standin
    standin.server.shutdown()


def two_rows(conn):
    add_event(conn, "a", "2026-11-10T10:05:00Z", tokens_in=10, tokens_out=1)
    add_event(conn, "b", "2026-11-10T10:30:00Z", tenant="t2", model="mB", tokens_in=7, tokens_out=3)


def test_close_sends_the_rollup_rows_as_one_json_each_row_insert(conn, ch):
    two_rows(conn)
    close_hour(conn, H)
    (query, body), = ch.inserts()
    assert query == "INSERT INTO usage_rollups_hourly FORMAT JSONEachRow"
    sent = sorted((json.loads(line) for line in body.splitlines()), key=lambda r: r["tenant_id"])
    assert [{k: v for k, v in r.items() if k != "version"} for r in sent] == [
        {"tenant_id": "t1", "model": "mA", "hour_start": "2026-11-10 10:00:00", "tokens_in": 10, "tokens_out": 1, "event_count": 1},
        {"tenant_id": "t2", "model": "mB", "hour_start": "2026-11-10 10:00:00", "tokens_in": 7, "tokens_out": 3, "event_count": 1},
    ]
    assert any("ReplacingMergeTree(version)" in q for q, _ in ch.requests)  # the table is created first


def test_clickhouse_500_does_not_fail_or_roll_back_the_close_and_is_logged(conn, ch, caplog):
    ch.status = 500
    two_rows(conn)
    with caplog.at_level(logging.ERROR):
        assert close_hour(conn, H) == (2, 2)
    assert len(rollups(conn)) == 2 and closed_hours(conn) == [H]
    assert "clickhouse mirror failed: hour=2026-11-10T10:00:00+00:00" in caplog.text and "500" in caplog.text


def test_refused_connection_does_not_fail_the_close_and_is_logged(conn, monkeypatch, caplog):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]  # closed again: nothing listens here
    monkeypatch.setenv("CLICKHOUSE_URL", f"http://127.0.0.1:{port}")
    two_rows(conn)
    with caplog.at_level(logging.ERROR):
        assert close_hour(conn, H) == (2, 2)
    assert closed_hours(conn) == [H]
    assert "clickhouse mirror failed: hour=" in caplog.text


def test_without_clickhouse_url_nothing_is_sent(conn, ch, monkeypatch):
    monkeypatch.delenv("CLICKHOUSE_URL")
    two_rows(conn)
    close_hour(conn, H)
    assert ch.requests == []


def test_backfill_sends_only_hours_that_differ_and_a_second_run_sends_nothing(conn, ch):
    h2 = datetime(2026, 11, 10, 11, tzinfo=timezone.utc)
    two_rows(conn)
    add_event(conn, "c", "2026-11-10T11:05:00Z", tokens_in=5, tokens_out=5)
    ch.status = 500  # both closes mirror nothing
    close_hour(conn, H)
    close_hour(conn, h2)
    ch.status = 200
    ch.requests.clear()
    assert mirror.backfill(conn, ch.url) == (2, [])
    assert len(ch.inserts()) == 2
    ch.requests.clear()
    # one row of the first hour changes in ClickHouse: only that hour is sent again
    key = next(k for k in ch.rows if k[0] == "t1")
    ch.rows[key]["tokens_in"] = 999
    assert mirror.backfill(conn, ch.url) == (1, [])
    (query, body), = ch.inserts()
    assert {json.loads(line)["hour_start"] for line in body.splitlines()} == {"2026-11-10 10:00:00"}
    assert len(body.splitlines()) == 2  # the whole hour, both rows
    ch.requests.clear()
    assert mirror.backfill(conn, ch.url) == (0, [])
    assert ch.inserts() == []


def test_backfill_reports_an_hour_that_still_fails_and_the_cli_exits_1(conn, ch, capsys):
    two_rows(conn)
    ch.status = 500
    close_hour(conn, H)
    with pytest.raises(SystemExit) as exit_:
        mirror.main(["--backfill"])
    assert exit_.value.code == 1


def test_export_actuals_writes_the_lines_reconcile_reads(conn, ch, tmp_path):
    two_rows(conn)
    close_hour(conn, H)
    path = tmp_path / "actuals.jsonl"
    assert mirror.export_actuals(ch.url, str(path)) == 2
    from app.reconcile import read_actuals
    assert read_actuals(str(path)) == {("t1", "mA", H): (10, 1), ("t2", "mB", H): (7, 3)}
