"""The demo scenario's independent check (scripts/scenario.py) and the stand-in inference server (scripts/stand_in_inference.py).

No database and no Docker: both scripts keep their pure logic importable. Scripts are loaded by path (scripts/ is not a package).
"""
import importlib.util
import json
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.seekrflow import MissingUsage, fetch_chat, usage_event

SCRIPTS = Path(__file__).parent.parent / "scripts"


def load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


scenario, stand_in = load("scenario"), load("stand_in_inference")
H = datetime(2026, 11, 10, 8, tzinfo=timezone.utc)
V1 = {"version": 1, "effective_from": datetime(2020, 1, 1, tzinfo=timezone.utc), "input": 1500, "output": 3000}


def micros(tokens_in, tokens_out, prices=None, hour=H):
    lines = scenario.expected_lines({("m", hour): (tokens_in, tokens_out)}, {"m": prices or [V1]})
    return lines[("m", hour)]["micros"]


def test_demo_numbers_match_demo_sh():
    assert micros(3000, 500) == 6000   # scripts/demo.sh step 7


def test_half_a_micro_rounds_up_and_just_under_rounds_down():
    assert micros(1, 0) == 2      # 1.5 micros
    assert micros(1, 0, [{**V1, "input": 1499}]) == 1   # 1.499
    assert micros(0, 1, [{**V1, "output": 1}]) == 0     # 0.001


def test_zero_tokens_cost_nothing_but_still_have_a_line():
    assert micros(0, 0) == 0


def test_rounding_happens_once_per_row_not_per_side():
    prices = [{**V1, "input": 400, "output": 400}]
    assert micros(1, 1, prices) == 1   # 0.8 once; per side it would be 0 + 0


def test_price_change_inside_an_hour_does_not_split_it():
    v2 = {"version": 2, "effective_from": H + timedelta(minutes=30), "input": 3000, "output": 3000}
    assert micros(1000, 0, [V1, v2], hour=H) == 1500                    # the hour started before the change: old price
    assert micros(1000, 0, [V1, v2], hour=H + timedelta(hours=1)) == 3000   # the next hour starts after it: new price


def test_a_tie_on_effective_from_goes_to_the_later_version():
    v2 = {**V1, "version": 2, "input": 2000}
    assert scenario.price_in_force([V1, v2], H)["version"] == 2


def test_no_price_in_force_raises_instead_of_pricing_at_zero():
    with pytest.raises(LookupError):
        scenario.price_in_force([{**V1, "effective_from": H + timedelta(hours=1)}], H)


def test_total_is_the_sum_of_the_rows():
    lines = scenario.expected_lines({("m", H): (1, 0), ("m", H + timedelta(hours=1)): (1, 0)}, {"m": [V1]})
    assert scenario.expected_total(lines) == 4


# ---- the stand-in server ----

def serve(seed):
    server = stand_in.make_server(0, seed)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def ask(server, path, body):
    request = urllib.request.Request(f"http://127.0.0.1:{server.server_port}{path}", json.dumps(body).encode(),
                                     {"Content-Type": "application/json"})
    with urllib.request.urlopen(request) as reply:
        return json.load(reply)


def run_requests(seed):
    server = serve(seed)
    try:
        base = f"http://127.0.0.1:{server.server_port}/v1"
        chats = [fetch_chat(base, "k", {"model": "m", "messages": []}) for _ in range(3)]
        embeddings = [ask(server, stand_in.EMBEDDINGS, {"model": "e", "input": "x"}) for _ in range(3)]
        return chats, embeddings
    finally:
        server.shutdown()


def test_same_seed_gives_the_same_token_counts_and_different_seeds_do_not():
    counts = lambda rs: [r["usage"] for r in rs[0] + rs[1]]
    assert counts(run_requests(7)) == counts(run_requests(7))
    assert counts(run_requests(7)) != counts(run_requests(8))
    assert len({u["prompt_tokens"] for u in counts(run_requests(7))}) > 1   # they vary per request


def test_every_body_is_accepted_by_the_adapter_and_ids_are_unique():
    chats, embeddings = run_requests(1)
    events = [usage_event(r, "t", "embed-model") for r in chats + embeddings]
    assert len({e["idempotency_key"] for e in events}) == 6
    assert all(e["tokens_in"] > 0 for e in events) and all(e["tokens_out"] == 0 for e in events[3:])
    assert all(e["model"] == "m" for e in events[:3]) and all(e["model"] == "embed-model" for e in events[3:])


def test_a_body_without_usage_still_raises():
    chats, _ = run_requests(1)
    del chats[0]["usage"]
    with pytest.raises(MissingUsage):
        usage_event(chats[0], "t")


def test_unknown_route_is_404():
    server = serve(1)
    try:
        with pytest.raises(urllib.error.HTTPError) as err:
            ask(server, "/nope", {})
        assert err.value.code == 404
    finally:
        server.shutdown()
