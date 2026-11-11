#!/usr/bin/env bash
# Database outage check against a LIVE `docker compose up` stack. NOT run by CI and NOT run by scripts/test.
# Stops the db through compose and expects 503 from POST /v1/usage; starts it and expects 201 again, with the event
# stored before the outage still there (a resend of it answers 200 and the table holds both). API_PORT picks the port;
# COMPOSE_PROJECT_NAME picks a non-default compose project. Exits non-zero on the first surprise.
set -euo pipefail
cd "$(dirname "$0")/.."

BASE="http://localhost:${API_PORT:-8000}"
TENANT=$(python3 scripts/flood.py --provision)
N=$(date +%s)
HOUR=$(date -u +%Y-%m-%dT%H:00:00Z)
post() { # post KEY -> the HTTP status ("000" when nothing answers)
  curl -s --connect-timeout 5 --max-time 30 -o /dev/null -w '%{http_code}' -X POST "$BASE/v1/usage" -H "X-Tenant-Id: $TENANT" -H 'Content-Type: application/json' \
    -d "{\"tenant_id\":\"$TENANT\",\"idempotency_key\":\"outage-$N-$1\",\"model\":\"demo-model\",\"tokens_in\":1,\"tokens_out\":1,\"occurred_at\":\"$HOUR\"}" || true
}
expect() { [ "$2" = "$3" ] || { echo "FAIL $1: expected $3, got $2"; exit 1; }; echo "$1 -> $2"; }
trap 'docker compose start db >/dev/null 2>&1 || true' EXIT   # never leave the stack without its database

expect "usage before the outage" "$(post before)" 201
docker compose stop db >/dev/null
expect "usage while the database is stopped" "$(post during)" 503
docker compose start db >/dev/null
code=000
for _ in $(seq 1 30); do code=$(post after); [ "$code" = 201 ] && break; sleep 1; done
expect "usage after the restart" "$code" 201
expect "resend of the event stored before the outage" "$(post before)" 200
ROWS=$(docker compose exec -T db psql -U metering -d metering -tAq -c "SELECT count(*) FROM usage_events WHERE tenant_id = '$TENANT'")
expect "rows stored for the tenant" "$ROWS" 2
echo "outage check ok"
