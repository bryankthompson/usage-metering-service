# Runbook: an invoice looks wrong

For on-call. Run from the repo on the host that runs `docker compose`. Every command below was run on a stack built from `main` and seeded by
`scripts/demo.sh`, plus a late event, an unpriced model, a past_due and a canceled tenant; the output is trimmed and the names are
from that run. Money is integer micro-USD (1,000,000 = 1 USD).

    export BASE=http://localhost:8000 T=demo-1794355718      # your tenant; BASE uses API_PORT
    psql() { docker compose exec -T db psql -U metering -d metering "$@"; }

## 1. Pin the claim
Write down: tenant `T`, period `[START, END)` in UTC (charges are per UTC hour), expected micros, billed micros (step 3).
Surprise: the customer quotes local time or a month: convert to UTC hours first. Next: 2.

## 2. Is the tenant entitled, and why
    curl -s -w ' [%{http_code}]\n' "$BASE/v1/tenants/$T/charges?start=START&end=END" -H "X-Tenant-Id: $T"
    psql -x -c "SELECT status, plan, stripe_customer_id, payment_failed_at, grace_until FROM entitlements WHERE tenant_id = '$T'"
    docker compose exec -T rollup python -m app.delinquent [--at ISO] [--json] [--fail-on-expired]
Expect 200 and `status | active` with empty `grace_until`. The edge answers 401 (no header), 402 (no row, `canceled`, or
`past_due` past `grace_until`: `{"detail":"not entitled: status canceled"}`) or 503 `{"detail":"entitlement check unavailable"}` (cannot read the database; usage, charges and the webhook all answer 503 while it is down
or restarting, and for a few seconds after a restart).
`app.delinquent` lists `past_due` and `canceled` tenants, denied ones first; `--at` asks "what at that time", `--json` is
one object per line, exit 1 only with `--fail-on-expired` and a denied `past_due` tenant:

    gone-co     canceled  canceled                     failed=- grace_until=-
    lapsed-co   past_due  in_grace           71h left  failed=2026-11-11T00:03:44+00:00 grace_until=2026-11-14T00:03:44+00:00
    ... --json --at 2026-11-20T00:00:00+00:00    {"tenant_id": "lapsed-co", "status": "past_due", "state": "grace_expired", ...,
                                                  "hours_left": null, "hours_ago": 143}

States: `in_grace` (allowed), `grace_expired` and `past_due_no_grace` (denied), `canceled` (denied). A denied tenant also
gets 402 from the charges route, so use the no-edge command in step 3. Surprise: a tenant that is missing here and gets 402
has no `entitlements` row: step 8. Next: 3.

## 3. What was billed
    curl -s "$BASE/v1/tenants/$T/charges?start=2026-11-10T22:00:00Z&end=2026-11-10T23:00:00Z" -H "X-Tenant-Id: $T"
    {"tenant_id":"demo-1794355718", ... "lines":[{"model":"demo-model","hour_start":"2026-11-10T22:00:00Z","tokens_in":3000,
     "tokens_out":500,"price_version":1,"micros":6000}],"total_micros":6000}
One line per hour and model, rated at the price in force at `hour_start`: `(in*in_price + out*out_price + 500) // 1000` per line,
total = sum of lines (3000*1500 + 500*3000 = 6,000,000, /1000 = 6000). `start` and `end` are ISO 8601 text with a time zone:
`start=5` is a 422 `must be ISO 8601 text with a time zone, such as 2026-11-01T00:00:00Z`. Without the edge (a denied tenant),
from the api container (one Python dict per line, then the total):

    docker compose exec -T api python -c "import sys; from datetime import datetime as d; from app import db, pricing; \
    l,t = pricing.charge_lines(db.connect(), sys.argv[1], d.fromisoformat(sys.argv[2]), d.fromisoformat(sys.argv[3])); \
    [print(x) for x in l]; print('total_micros', t)" $T 2026-11-10T22:00:00+00:00 2026-11-10T23:00:00+00:00

Surprises: `lines: []` means no closed rollup row in the period (hours still open, no events, or all late): step 5.
409 `{"detail":"no price for model 'mystery-model' at 2026-11-10T21:00:00+00:00"}` refuses the whole range, never a partial
bill: narrow START/END hour by hour to find it, then step 6. Next: 4.

## 4. Do the rollups match the raw log
    docker compose exec -T rollup python -m app.reconcile [--hour 2026-11-10T22:00:00Z | --since ISO] [--tolerance-bps 100]
    docker compose exec -T rollup python -m app.reconcile --hour 2026-11-10T22:00:00Z --actuals /dev/stdin < actuals.jsonl
Expect `reconcile: hours=1 rows=1 drift=0`, exit 0. `actuals.jsonl` is one object per line: `tenant_id, model, hour, tokens_in,
tokens_out`. Exit 1 = drift, 2 = bad argument or file (`--hour` must be a closed hour: `... is not a closed hour`). Drift lines:

    DRIFT tenant=demo-... model=demo-model hour=2026-11-10T22:00:00Z tokens_in rollup=3007 events=3000
    DRIFT ... events but no rollup row (events=(3000, 500, 2))
    DRIFT tenant=ghost-co ... rollup row but no events (rollup=(10, 10, 1))
    DRIFT ... beyond 100 bps: tokens_in rollup=3000 actual=3500 diff=-500
    DRIFT ... rollup row but no actual (rollup=(3000, 500))      DRIFT ... actual but no rollup row (actual=(10, 10))
Different sums, a missing row and an extra row mean the table and the log disagree: the table was changed outside `app.rollup`
or the rollup code is wrong (the close is one transaction, so a failed close leaves no row). Do not edit it; escalate with the line. A `beyond N bps`, `... no actual` or `actual but no
rollup row` line compares with the other source: the usage the customer or Stripe saw was not all ingested, or was late (5).
Next: 5 if the numbers agree with the log but not the customer.

## 5. Accepted but not billed
    psql -c "SELECT e.id, e.idempotency_key, e.tokens_in, e.tokens_out, e.occurred_at, e.received_at, c.closed_at
             FROM usage_events e JOIN closed_hours c ON c.hour_start = date_trunc('hour', e.occurred_at, 'UTC')
             WHERE e.tenant_id = '$T' AND e.late ORDER BY e.id"
     4 | late-1 | 400 | 100 | 2026-11-10 22:45:00+00 | 2026-11-11 00:08:44+00 | 2026-11-11 00:08:38+00
A late event was stored but its hour was already closed when it arrived (`received_at` after `closed_at`); it is never in a
rollup, never billed, and no code reopens the hour. Per hour, billed against late:

    psql -c "SELECT date_trunc('hour', occurred_at, 'UTC') AS hour, count(*) FILTER (WHERE NOT late) AS counted,
             count(*) FILTER (WHERE late) AS late FROM usage_events WHERE tenant_id = '$T' GROUP BY 1 ORDER BY 1"
     2026-11-10 22:00:00+00 | 2 | 1
Events in an hour with no `closed_hours` row are simply not closed yet: the hour is due 5 minutes after it ends and the
`rollup` service runs every 3600 s. Open hours of the tenant:

    psql -c "SELECT date_trunc('hour', e.occurred_at, 'UTC') AS hour, count(*) FROM usage_events e WHERE e.tenant_id = '$T'
             AND NOT EXISTS (SELECT 1 FROM closed_hours c WHERE c.hour_start = date_trunc('hour', e.occurred_at, 'UTC')) GROUP BY 1"
Known cause: a closed hour is closed for every tenant, and any tenant's event in an old quiet hour makes the next rollup close
it, so another tenant's later event for that hour is late. Billing late events is a decision for the business, not a command.

## 6. Prices
    psql -c "SELECT model, version, effective_from, input_micros_per_1k, output_micros_per_1k FROM price_versions ORDER BY model, version"
The version used is the greatest `effective_from <= hour_start` (a tie goes to the later version), so a price that starts mid-hour
applies from the next hour. Compare with the `price_version` on the line; `pricing.price_at(conn, model, at)` returns it, or
raises `NoPrice: no price for model 'mystery-model' at ...` (never rated as zero). A missing price is added with `app.pricing.add_price_version(conn, model, effective_from, in_per_1k, out_per_1k)`; there is no
CLI or API for it, and a version dated before invoiced hours changes what those hours would be rated at.

## 7. A missing or doubled event
    psql -c "SELECT id, idempotency_key, model, tokens_in, tokens_out, occurred_at, received_at, late FROM usage_events
             WHERE tenant_id = '$T' AND idempotency_key = 'late-1'"
`POST /v1/usage` answers 201 `accepted` (stored), 200 `duplicate` (same key and payload: nothing stored), 409 (`idempotency_key was already used with a
different payload`; nothing stored), 403 (`tenant_id does not match the entitled tenant`), 422 (invalid body, or `occurred_at` not ISO text), or an edge 401/402/503.
The log
(`docker compose logs api | grep -E "usage (accepted|duplicate|conflict|rejected)|edge denied"`):

    INFO:     usage accepted: tenant=demo-... key='late-1' event_id=4 late=True       (a resend: `usage duplicate:` ...)
    WARNING:  usage conflict: tenant=demo-... key='late-1' reused with a different payload (event_id=4)
    WARNING:  usage rejected: body tenant 'lapsed-co' is not the entitled tenant 'demo-...'
    2026-11-11 00:08:38,562 WARNING app.edge edge denied: tenant='nobody-...' status=402 reason=no entitlement
Missing: no row, so it was never accepted; ask the client for the status it saw. The hour is the UTC hour of `occurred_at`,
not `received_at`. Doubled: the service dedupes only on (tenant, key); two keys for one real call are two events. Look-alikes:

    psql -c "SELECT model, tokens_in, tokens_out, occurred_at, array_agg(idempotency_key ORDER BY id) AS keys FROM usage_events
             WHERE tenant_id = '$T' GROUP BY 1,2,3,4 HAVING count(*) > 1"
     demo-model | 400 | 100 | 2026-11-10 22:45:00+00 | {late-1,late-1-retry}
A doubled event cannot be deleted (a trigger refuses UPDATE and DELETE) and there is no adjustment feature: escalate for a manual credit.

## 8. Webhooks and grace
    psql -c "SELECT event_id, type, received_at FROM stripe_events ORDER BY received_at"
    psql -c "SELECT tenant_id, status, stripe_customer_id AS customer, payment_failed_at, grace_until, last_event_at FROM entitlements ORDER BY tenant_id"
A signed event is `processed` (state changed), `stale` (older than the last applied event for that tenant: ignored, but recorded),
`duplicate` (event id already in `stripe_events`) or `ignored` (no tenant in the metadata, an unknown customer, an unhandled event
type, or a failed payment for a `canceled` tenant). The HTTP answer is `{"status": ...}`. `processed` and `stale` leave a
`stripe_events` row (`evt2_t2_old`, the stale one, is in the table); a `duplicate` adds nothing and an `ignored` event is not
recorded in the table, so read the outcome line (INFO, `LOG_LEVEL` overrides; `tenant` is missing when none was found):

    docker compose logs api | grep -E "webhook (processed|stale|duplicate|ignored|refused)"
    2026-11-11 00:08:44,378 INFO app.webhooks webhook duplicate: event='evt2_t2_fail1' type='invoice.payment_failed'
    2026-11-11 00:08:44,391 INFO app.webhooks webhook stale: event='evt2_t2_old' type='customer.subscription.updated' tenant='lapsed-co'
    2026-11-11 00:08:44,405 INFO app.webhooks webhook ignored: event='evt2_unknown' type='invoice.payment_failed' tenant=None
    2026-11-11 00:12:46,877 WARNING app.webhooks webhook refused: No signatures found matching the expected signature for payload

or Stripe's delivery log, which shows our response body. Refusals: 400 `invalid signature or payload` (bad signature), 413
`webhook body too large` (over 1 MiB), 503 `database unavailable` (database down), 503 `webhook secret not configured` (no or blank
`STRIPE_WEBHOOK_SECRET`) or 503 `grace period not configured` (`GRACE_PERIOD_HOURS` out of range). A payment failure opens grace for `GRACE_PERIOD_HOURS` (default 72, 0 to 8760) from the FIRST failure; later retries do not
extend it; an `active` event clears it; a cancel clears it and nothing but a subscription event reopens access.

## Symptom | most likely cause | where to look
| Symptom | Most likely cause | Look at |
|--|--|--|
| Total is 0 or `lines: []` | hour not closed yet, events all late, or no events | 5 |
| 409 `no price for model` on charges | a model with no price version in force at that hour | 6 |
| Customer says usage is higher than billed | late events, or events in an open hour | 5, then 4 with `--actuals` |
| Customer says usage is lower than billed | two keys for one call, or the wrong price version | 7, 6 |
| Billed amount differs by 1 micro | per-line half-up rounding, summed once per line | 3 |
| 503 on any route | database stopped or restarting (back in a few seconds) | `docker compose ps db`; the `database unavailable` log line |
| 422 on charges | `start` or `end` is not ISO text with a time zone | 3 |
| 402 on usage or charges | no entitlement row, canceled, or grace expired | 2, 8 |
| `reconcile` exits 1 | rollup table changed outside `app.rollup`, or the other source differs | 4 |
| Tenant paid but still denied | only a `customer.subscription.*` event reopens access; `invoice.paid` does not | 8 |
