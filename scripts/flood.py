#!/usr/bin/env python3
"""Burst check against a LIVE `docker compose up` stack. NOT run by CI and NOT run by scripts/test.

    python3 scripts/flood.py [REQUESTS=600] [CLIENTS=150]   # API_PORT (default 8000) picks the port
    python3 scripts/flood.py --provision                    # print a fresh entitled tenant id and stop

Makes a fresh tenant (a signed subscription event, with the fake dev secret from compose.yaml), then sends REQUESTS
usage events from CLIENTS threads over REQUESTS/2 keys, so half are resends. Passes only if the answers are exactly
REQUESTS/2 x 201 and the rest 200 (no 503, no 500) and the api log gained no "Traceback".
"""

import collections, concurrent.futures as cf, hashlib, hmac, json, os, subprocess, sys, time, urllib.error, urllib.request
from datetime import datetime, timezone

BASE = f"http://localhost:{os.environ.get('API_PORT') or '8000'}"
SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "whsec_test_only_not_a_real_secret")  # the fake secret in compose.yaml


def post(path, body, headers):
    request = urllib.request.Request(BASE + path, body, {"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status
    except urllib.error.HTTPError as err:
        return err.code
    except OSError as err:  # a refused connection is a result to report, not a crash
        return type(err).__name__


def provision():
    n = str(time.time_ns())
    sub = {
        "id": "sub_" + n,
        "object": "subscription",
        "customer": "cus_" + n,
        "status": "active",
        "metadata": {"tenant_id": "flood-" + n},
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
    return "flood-" + n


def tracebacks():
    logs = subprocess.run(["docker", "compose", "logs", "api"], capture_output=True, text=True)
    if logs.returncode:
        sys.exit("FAIL cannot read the api log: " + logs.stderr.strip())
    return logs.stdout.count("Traceback")


if sys.argv[1:] == ["--provision"]:
    print(provision())
    sys.exit(0)
try:
    total = int(sys.argv[1]) if len(sys.argv) > 1 else 600
    clients = int(sys.argv[2]) if len(sys.argv) > 2 else 150
except ValueError:
    sys.exit("usage: flood.py [REQUESTS>=2] [CLIENTS>=1] (integers)")
if total < 2 or clients < 1:
    sys.exit("usage: flood.py [REQUESTS>=2] [CLIENTS>=1] (integers)")
tenant, before, run = provision(), tracebacks(), time.time_ns()
hour = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:00:00Z")


def send(i):
    body = {
        "tenant_id": tenant,
        "idempotency_key": f"{run}-{i % (total // 2)}",
        "model": "demo-model",
        "tokens_in": 1,
        "tokens_out": 1,
        "occurred_at": hour,
    }
    return post("/v1/usage", json.dumps(body).encode(), {"X-Tenant-Id": tenant})


with cf.ThreadPoolExecutor(clients) as pool:
    answers = dict(collections.Counter(pool.map(send, range(total))))
new = tracebacks() - before
print(f"{total} requests from {clients} clients: {answers}, new tracebacks: {new}")
if answers != {201: total // 2, 200: total - total // 2} or new:
    sys.exit(f"FAIL: expected {total // 2} x 201, {total - total // 2} x 200 and no new tracebacks")
print("flood ok")
