#!/usr/bin/env python3
"""Ingest throughput against a LIVE `docker compose up` stack. NOT run by CI and NOT run by scripts/test.

    python3 scripts/throughput.py [--mode single|batch] [--events 2000] [--clients 8] [--batch-size 100] [--runs 1] [--warmup 0]

API_PORT (default 8000) picks the port. Makes one fresh entitled tenant (as scripts/flood.py does), then per run sends
--events usage events from --clients threads, every key new (nothing is a resend): one POST /v1/usage per event, or
POST /v1/usage/batch with --batch-size events per request. Prints per run the events accepted, wall seconds, events per
second, and the p50/p95/p99 latency of a request in ms (a batch request carries --batch-size events), then the status
counts. --warmup runs go first and are printed but marked as warm-up. Exits 1 on any status other than 201 or any
connection error. The figures describe this machine at this commit, nothing more.
"""

import argparse, collections, concurrent.futures as cf, hashlib, hmac, json, math, os, sys, time, urllib.error, urllib.request
from datetime import datetime, timezone

BASE = f"http://localhost:{os.environ.get('API_PORT') or '8000'}"
SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "whsec_test_only_not_a_real_secret")  # the fake secret in compose.yaml


def post(path, body, headers):
    request = urllib.request.Request(BASE + path, body, {"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            response.read()
            return response.status
    except urllib.error.HTTPError as err:
        return err.code
    except OSError as err:  # a refused connection is a result to report, not a crash
        return type(err).__name__


def provision():  # the same signed subscription event as scripts/flood.py
    n = str(time.time_ns())
    sub = {
        "id": "sub_" + n,
        "object": "subscription",
        "customer": "cus_" + n,
        "status": "active",
        "metadata": {"tenant_id": "tput-" + n},
        "items": {"data": [{"price": {"id": "price_flood"}}]},
    }
    payload = json.dumps(
        {
            "id": "evt_" + n,
            "object": "event",
            "type": "customer.subscription.updated",
            "created": int(time.time()),
            "data": {"object": sub},
        }
    ).encode()
    stamp = int(time.time())
    sig = hmac.new(SECRET.encode(), f"{stamp}.".encode() + payload, hashlib.sha256).hexdigest()
    status = post("/v1/stripe/webhook", payload, {"Stripe-Signature": f"t={stamp},v1={sig}"})
    if status != 200:
        sys.exit(f"FAIL provisioning: the webhook answered {status} (is the stack up on {BASE}?)")
    return "tput-" + n


def percentile(sorted_ms, p):
    return sorted_ms[max(0, math.ceil(p / 100 * len(sorted_ms)) - 1)]


def one_run(args, tenant, label):
    run = time.time_ns()
    hour = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:00:00Z")
    headers = {"X-Tenant-Id": tenant}

    def event(i):
        return {
            "tenant_id": tenant,
            "idempotency_key": f"{run}-{i}",
            "model": "demo-model",
            "tokens_in": 1,
            "tokens_out": 1,
            "occurred_at": hour,
        }

    if args.mode == "single":
        requests = [("/v1/usage", json.dumps(event(i)).encode(), 1) for i in range(args.events)]
    else:
        size = args.batch_size
        requests = [
            (
                "/v1/usage/batch",
                json.dumps({"events": [event(i) for i in range(s, min(s + size, args.events))]}).encode(),
                min(size, args.events - s),
            )
            for s in range(0, args.events, size)
        ]

    def send(req):
        path, body, count = req
        start = time.perf_counter()
        status = post(path, body, headers)
        return status, count, (time.perf_counter() - start) * 1000

    begin = time.perf_counter()
    with cf.ThreadPoolExecutor(args.clients) as pool:
        results = list(pool.map(send, requests))
    wall = time.perf_counter() - begin
    statuses = dict(collections.Counter(status for status, _, _ in results))
    accepted = sum(count for status, count, _ in results if status == 201)
    ms = sorted(m for _, _, m in results)
    print(
        f"{label} mode={args.mode} clients={args.clients} accepted={accepted} wall_s={wall:.2f} events_per_s={accepted / wall:.0f} "
        f"p50_ms={percentile(ms, 50):.1f} p95_ms={percentile(ms, 95):.1f} p99_ms={percentile(ms, 99):.1f} statuses={statuses}",
        flush=True,
    )
    return statuses == {201: len(requests)}


parser = argparse.ArgumentParser(description="ingest throughput against a live stack")
parser.add_argument("--mode", choices=["single", "batch"], default="single")
parser.add_argument("--events", type=int, default=2000)
parser.add_argument("--clients", type=int, default=8)
parser.add_argument("--batch-size", type=int, default=100)
parser.add_argument("--runs", type=int, default=1)
parser.add_argument("--warmup", type=int, default=0)
args = parser.parse_args()
if min(args.events, args.clients, args.batch_size, args.runs) < 1 or args.warmup < 0 or args.batch_size > 1000:
    sys.exit("usage: --events, --clients, --runs >= 1; 1 <= --batch-size <= 1000; --warmup >= 0")
tenant = provision()
ok = True
for i in range(args.warmup):
    ok &= one_run(args, tenant, f"warmup {i + 1}/{args.warmup}")
for i in range(args.runs):
    ok &= one_run(args, tenant, f"run {i + 1}/{args.runs}")
if not ok:
    sys.exit("FAIL: a request did not answer 201 (see statuses above)")
