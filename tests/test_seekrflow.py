"""Platform adapter tests (app/seekrflow.py).

The fixtures in tests/fixtures/seekr_*.json are HAND-WRITTEN from the public documented schema. They are not
recorded from the service: no call to it has been made. fetch_chat is exercised only against a stand-in
http.server on 127.0.0.1. Prices used here are ILLUSTRATIVE, not the platform's.
"""
import json
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
import uvicorn

from app import seekrflow
from app.main import app
from app.pricing import add_price_version, charge_lines
from app.rollup import close_hour
from app.seekrflow import MissingUsage, fetch_chat, post_usage, usage_event

FIXTURES = Path(__file__).parent / "fixtures"
TENANT = "tenant-1"
HOUR = datetime(2026, 7, 10, 10, tzinfo=timezone.utc)   # every fixture's `created` falls in this UTC hour


def load(name):
    return json.loads((FIXTURES / name).read_text())


CHAT, CACHED, EMB, CHUNK = (load(f"seekr_{n}.json") for n in ("chat", "chat_cached", "embeddings", "stream_final_chunk"))


def test_chat_maps_to_one_event():
    assert usage_event(CHAT, TENANT) == {
        "tenant_id": TENANT, "idempotency_key": "chatcmpl-hand-0001", "model": "meta-llama/Llama-3.1-8B-Instruct",
        "tokens_in": 1000, "tokens_out": 500, "occurred_at": "2026-07-10T10:05:00+00:00"}


def test_embeddings_have_no_output_tokens_and_take_the_model_argument():
    event = usage_event(EMB, TENANT, model="BAAI/bge-large-en-v1.5")
    assert (event["tokens_in"], event["tokens_out"], event["model"]) == (501, 0, "BAAI/bge-large-en-v1.5")
    assert event["idempotency_key"] == "emb-hand-0003"


def test_final_stream_chunk_maps_like_a_response():
    event = usage_event(CHUNK, TENANT)
    assert (event["idempotency_key"], event["tokens_in"], event["tokens_out"]) == ("chatcmpl-hand-0004", 40, 60)


def test_cached_tokens_stay_inside_prompt_tokens():
    assert usage_event(CACHED, TENANT)["tokens_in"] == 2000   # cached_tokens (1500) is not subtracted or added


def test_created_becomes_a_zoned_iso_time():
    parsed = datetime.fromisoformat(usage_event(CHAT, TENANT)["occurred_at"])
    assert parsed.utcoffset() is not None and parsed == datetime(2026, 7, 10, 10, 5, tzinfo=timezone.utc)


@pytest.mark.parametrize("break_it", [
    lambda r: r.pop("usage"),
    lambda r: r.update(usage=None),
    lambda r: r["usage"].pop("prompt_tokens"),
    lambda r: r["usage"].pop("completion_tokens"),
    lambda r: r["usage"].update(completion_tokens=None),
    lambda r: r["usage"].update(prompt_tokens="12"),
    lambda r: r["usage"].update(prompt_tokens=1.5),
    lambda r: r["usage"].update(prompt_tokens=-1),
    lambda r: r.pop("created"),
])
def test_missing_or_bad_usage_raises_and_is_never_zero(break_it):
    response = json.loads(json.dumps(CHAT))
    break_it(response)
    with pytest.raises(MissingUsage):
        usage_event(response, TENANT)


def test_fixture_without_usage_raises():
    with pytest.raises(MissingUsage):
        usage_event(load("seekr_chat_no_usage.json"), TENANT)


@pytest.fixture
def api(conn):
    """The real app on 127.0.0.1 (real edge, real database), so post_usage goes over real HTTP."""
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        while not server.started:
            thread.join(0.01)
        conn.execute("INSERT INTO entitlements (tenant_id, status) VALUES (%s, 'active')", (TENANT,))
        conn.commit()
        yield f"http://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}"
    finally:
        server.should_exit = True
        thread.join(10)


def rows(conn):
    return conn.execute("SELECT count(*) AS n FROM usage_events").fetchone()["n"]


def test_post_stores_once_and_a_second_post_is_a_duplicate(api, conn):
    event = usage_event(CHAT, TENANT)
    assert post_usage(event, api, TENANT)[0] == 201
    status, body = post_usage(event, api, TENANT)
    assert (status, body["status"]) == (200, "duplicate") and rows(conn) == 1


def test_rollup_and_charge_are_exact_integers(api, conn):
    # Illustrative micro-USD per 1,000 tokens (in, out), not the platform's prices.
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    add_price_version(conn, "meta-llama/Llama-3.1-8B-Instruct", start, 2000, 4000)
    add_price_version(conn, "meta-llama/Llama-3.3-70B-Instruct", start, 3000, 6000)
    add_price_version(conn, "BAAI/bge-large-en-v1.5", start, 1500, 0)
    for response, model in ((CHAT, None), (CHUNK, None), (CACHED, None), (EMB, "BAAI/bge-large-en-v1.5")):
        assert post_usage(usage_event(response, TENANT, model), api, TENANT)[0] == 201
    assert close_hour(conn, HOUR) == (3, 4)
    lines, total = charge_lines(conn, TENANT, HOUR, datetime(2026, 7, 10, 11, tzinfo=timezone.utc))
    assert {ln["model"]: ln["micros"] for ln in lines} == {
        "meta-llama/Llama-3.1-8B-Instruct": 4320,    # (1040 * 2000 + 560 * 4000) / 1000
        "meta-llama/Llama-3.3-70B-Instruct": 6600,   # (2000 * 3000 + 100 * 6000) / 1000: cached tokens at the full input price
        "BAAI/bge-large-en-v1.5": 752,               # 501 * 1500 / 1000 = 751.5, rounded half up
    }
    assert total == 11672 and isinstance(total, int)


class StandIn(BaseHTTPRequestHandler):
    seen = {}

    def do_POST(self):
        StandIn.seen = {"path": self.path, "auth": self.headers["Authorization"],
                        "body": json.loads(self.rfile.read(int(self.headers["Content-Length"])))}
        payload = json.dumps(CHAT).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


@pytest.fixture
def stand_in():
    server = HTTPServer(("127.0.0.1", 0), StandIn)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/v1"
    server.shutdown()


def test_fetch_chat_sends_the_documented_path_and_bare_key_header(stand_in):
    reply = fetch_chat(stand_in, "key-123", {"model": "m", "messages": []})
    assert reply == CHAT
    assert StandIn.seen["path"] == "/v1/inference/chat/completions"
    assert StandIn.seen["auth"] == "key-123"   # no "Bearer"


def test_cli_replay_posts_a_list_and_a_resend_is_a_duplicate(api, conn, tmp_path, capsys):
    saved = tmp_path / "saved.json"
    saved.write_text(json.dumps([CHAT, CHUNK, CHAT]))
    assert seekrflow.main(["replay", str(saved), "--tenant", TENANT, "--api", api]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert [ln.split()[-1] for ln in lines] == ["accepted", "accepted", "duplicate"]
    assert lines[0] == "chatcmpl-hand-0001 in=1000 out=500 201 accepted" and rows(conn) == 2


def test_cli_replay_exits_non_zero_on_missing_usage_and_posts_nothing(api, conn, capsys):
    path = str(FIXTURES / "seekr_chat_no_usage.json")
    assert seekrflow.main(["replay", path, "--tenant", TENANT, "--api", api]) == 1
    assert "usage" in capsys.readouterr().err and rows(conn) == 0


def test_cli_replay_exits_non_zero_when_the_intake_refuses(api, conn, capsys):
    assert seekrflow.main(["replay", str(FIXTURES / "seekr_chat.json"), "--tenant", "nobody", "--api", api]) == 1
    assert "402" in capsys.readouterr().err


def test_cli_live_without_a_key_fails_and_prints_no_secret(monkeypatch, capsys):
    monkeypatch.delenv("SEEKR_API_KEY", raising=False)
    assert seekrflow.main(["live", "--tenant", TENANT, "--prompt", "hi"]) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and "SEEKR_API_KEY is not set" in captured.err


def test_cli_live_against_the_stand_in_never_prints_the_key(api, stand_in, monkeypatch, capsys):
    monkeypatch.setenv("SEEKR_API_KEY", "key-SECRET-xyz")
    assert seekrflow.main(["live", "--tenant", TENANT, "--prompt", "hi", "--api", api, "--base", stand_in]) == 0
    captured = capsys.readouterr()
    assert "key-SECRET-xyz" not in captured.out + captured.err and StandIn.seen["auth"] == "key-SECRET-xyz"


def test_cli_live_spools_the_response_before_posting_only_when_asked(api, stand_in, monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("SEEKR_API_KEY", "key-SECRET-xyz")
    spool = tmp_path / "saved.json"
    # a refused post (unknown tenant) still leaves the response on disk, so the usage can be replayed
    assert seekrflow.main(["live", "--tenant", "nobody", "--prompt", "hi", "--api", api, "--base", stand_in,
                           "--spool", str(spool)]) == 1
    assert json.loads(spool.read_text())["usage"]["prompt_tokens"] >= 0
    assert "key-SECRET-xyz" not in capsys.readouterr().err
    assert seekrflow.main(["replay", str(spool), "--tenant", TENANT, "--api", api]) == 0


def test_cli_live_writes_no_file_unless_asked(api, stand_in, monkeypatch, tmp_path):
    monkeypatch.setenv("SEEKR_API_KEY", "key-SECRET-xyz")
    monkeypatch.chdir(tmp_path)
    assert seekrflow.main(["live", "--tenant", TENANT, "--prompt", "hi", "--api", api, "--base", stand_in]) == 0
    assert list(tmp_path.iterdir()) == []
