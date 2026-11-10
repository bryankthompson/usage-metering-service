#!/usr/bin/env bash
# Drive a running `docker compose up` stack: price -> entitlement -> usage -> rollup -> charges.
# Re-runnable: each run uses a fresh tenant, fresh keys and the latest hour that is not closed yet.
set -euo pipefail
cd "$(dirname "$0")/.."

BASE="http://localhost:${API_PORT:-8000}"
SECRET=whsec_test_only_not_a_real_secret   # the fake secret from compose.yaml
N=$(date +%s)
TENANT="demo-$N"
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
psql() { docker compose exec -T db psql -U metering -d metering -tAq "$@"; }

# call STEP EXPECTED_STATUS curl-args...: body goes to $TMP/body, a mismatch stops the script
call() {
  local step=$1 want=$2 got; shift 2
  : > "$TMP/body"
  got=$(curl -s -o "$TMP/body" -w '%{http_code}' "$@" || true)   # a refused connection reads as 000, not a silent exit
  if [ "$got" != "$want" ]; then echo "FAIL step $step: expected $want, got $got"; cat "$TMP/body"; echo; exit 1; fi
  echo "step $step: $STEP_WHAT -> $got"
}
usage() { # usage KEY TOKENS_IN TOKENS_OUT TIME
  printf '{"tenant_id":"%s","idempotency_key":"%s-%s","model":"demo-model","tokens_in":%s,"tokens_out":%s,"occurred_at":"%s"}' \
    "$TENANT" "$TENANT" "$1" "$2" "$3" "$4"
}

# 1. the latest UTC hour that started 2+ hours ago and is not closed
IFS='|' read -r HOUR NEXT ISO < <(psql -F '|' -c "
  SELECT to_char(h, 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"'), to_char(h + interval '1 hour', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"'),
         to_char(h, 'YYYY-MM-DD\"T\"HH24:MI:SS\"+00:00\"')
  FROM generate_series(date_trunc('hour', now(), 'UTC') - interval '2 hours',
                       date_trunc('hour', now(), 'UTC') - interval '200 hours', interval '-1 hour') h
  WHERE NOT EXISTS (SELECT 1 FROM closed_hours WHERE hour_start = h) LIMIT 1")
[ -n "$HOUR" ] || { echo "FAIL step 1: no open hour in the last 200 hours"; exit 1; }
echo "step 1: hour $HOUR, tenant $TENANT -> ok"
T=${HOUR%:00:00Z}   # e.g. 2026-11-10T08

# 2. a price version for the demo model
psql -c "INSERT INTO price_versions (model, version, effective_from, input_micros_per_1k, output_micros_per_1k)
         VALUES ('demo-model', 1, '2020-01-01T00:00:00Z', 1500, 3000) ON CONFLICT DO NOTHING" >/dev/null
echo "step 2: price demo-model v1 (1500 in, 3000 out per 1k) -> ok"

# 3. a signed subscription event; the signature covers the exact bytes curl sends
SIG=$(python3 - "$SECRET" "$TENANT" "$N" "$TMP/event.json" <<'PY'
import hashlib, hmac, json, sys, time
secret, tenant, n, path = sys.argv[1:]
now = int(time.time())
sub = {"id": "sub_" + n, "object": "subscription", "customer": "cus_" + n, "status": "active",
       "metadata": {"tenant_id": tenant}, "items": {"data": [{"price": {"id": "price_demo"}}]}}
payload = json.dumps({"id": "evt_" + n, "object": "event", "type": "customer.subscription.updated",
                      "created": now, "data": {"object": sub}}).encode()
open(path, "wb").write(payload)
print(f"t={now},v1=" + hmac.new(secret.encode(), f"{now}.".encode() + payload, hashlib.sha256).hexdigest())
PY
)
STEP_WHAT="signed subscription.updated"
call 3 200 -X POST "$BASE/v1/stripe/webhook" -H 'Content-Type: application/json' \
  -H "Stripe-Signature: $SIG" --data-binary "@$TMP/event.json"
grep -q '"status":"processed"' "$TMP/body" || { echo "FAIL step 3: expected processed, got $(cat "$TMP/body")"; exit 1; }

# 4. the edge refuses an unknown tenant (402) and a missing tenant (401)
STEP_WHAT="unknown tenant"
call 4 402 -X POST "$BASE/v1/usage" -H "X-Tenant-Id: nobody-$N" -H 'Content-Type: application/json' -d "$(usage x 1 1 "${T}:10:00Z")"
STEP_WHAT="no tenant header"
call 4 401 -X POST "$BASE/v1/usage" -H 'Content-Type: application/json' -d "$(usage x 1 1 "${T}:10:00Z")"

# 5. three usage events in the hour: new, new, resend of the first
E1=$(usage 1 1000 500 "${T}:10:00Z"); E2=$(usage 2 2000 0 "${T}:20:00Z")
post() { call 5 "$1" -X POST "$BASE/v1/usage" -H "X-Tenant-Id: $TENANT" -H 'Content-Type: application/json' -d "$2"; }
STEP_WHAT="usage 1000 in / 500 out"; post 201 "$E1"
STEP_WHAT="usage 2000 in / 0 out"; post 201 "$E2"
STEP_WHAT="resend of usage 1 (duplicate)"; post 200 "$E1"

# 6. close the hour (the rollup closes hours that ended 5+ minutes ago)
OUT=$(docker compose exec -T rollup python -m app.rollup)
echo "$OUT"
# the rollup closes every unclosed hour in the database, so other tenants' rows may share the line: check our hour
# closed with at least our row and events; the exact 6000 total in step 7 is the per-tenant check
echo "$OUT" | grep -q "^closed hour=$ISO rows=" \
  || { echo "FAIL step 6: expected a 'closed hour=$ISO' line, got: $OUT"; exit 1; }
echo "step 6: rollup -> closed hour=$ISO"

# 7. charges: 3000*1500 + 500*3000 = 6,000,000 / 1000 = 6000 micros
STEP_WHAT="charges for the hour"
call 7 200 "$BASE/v1/tenants/$TENANT/charges?start=$HOUR&end=$NEXT" -H "X-Tenant-Id: $TENANT"
python3 -m json.tool "$TMP/body"
TOTAL=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["total_micros"])' "$TMP/body")
[ "$TOTAL" = 6000 ] || { echo "FAIL step 7: expected total_micros 6000, got $TOTAL"; cat "$TMP/body"; exit 1; }
echo "demo ok"
