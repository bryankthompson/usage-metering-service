# Runbook: an invoice looks wrong

For on-call. Run from the repo on the host that runs `docker compose`. Every command below was run on a stack built from
`main` and seeded by `scripts/demo.sh`, plus a late event, two unpriced models, a past_due and a canceled tenant, an hour that
cannot close, a stopped database and a paused one; the output is trimmed and the names are from that run. Those marked "not run
here" were not run. Square brackets mark options; capitals (`START`, `MODEL`) are for you to fill in. The repo names no owner or
channel for this service: where a step says to ask, ask whoever runs it. Money is integer micro-USD (1,000,000 = 1 USD).

    export BASE=http://localhost:8000 T=demo-1794359437      # T: the customer's tenant id (scripts/demo.sh prints its own on its step 1 line); BASE uses API_PORT
    psql() { docker compose exec -T db psql -U metering -d metering "$@"; }
The tenant and the dates in the examples below (2026-11-10 and 11) come from one run: use your tenant and your UTC hours.
The SQL examples pass the tenant to `psql` as a variable (`-v t=...`, used as `:'t'`), so `psql` quotes the id and an id with a single quote works there.

## 1. Pin the claim
Write down: tenant `T`, period `[START, END)` in UTC (charges are per UTC hour), expected micros, billed micros (step 3).
Surprise: the customer quotes local time or a month: convert to UTC hours first. Next: 2.

## 2. Is the tenant entitled, and why
    curl -s -w ' [%{http_code}]\n' "$BASE/v1/tenants/$T/charges?start=START&end=END" -H "X-Tenant-Id: $T"
    psql -x -v t="$T" <<'SQL'
    SELECT status, plan, stripe_customer_id, payment_failed_at, grace_until FROM entitlements WHERE tenant_id = :'t';
    SQL
    docker compose exec -T rollup python -m app.delinquent [--at ISO] [--json] [--fail-on-expired]
Expect 200 and `status | active` with empty `grace_until`. The edge answers 401 (no header), 402 (no row, `canceled`, or
`past_due` past `grace_until`: `{"detail":"not entitled: status canceled"}`) or 503 `{"detail":"entitlement check unavailable"}`
(it cannot read the database: step 11). `app.delinquent` lists `past_due` and `canceled` tenants, denied ones first; `--at` asks
"what at that time", `--json` is one object per line, exit 1 only with `--fail-on-expired` and a denied `past_due` tenant:

    gone-co     canceled  canceled                     failed=- grace_until=-
    lapsed-co   past_due  in_grace           71h left  failed=2026-11-11T01:05:47+00:00 grace_until=2026-11-14T01:05:47+00:00
    ... --json --at 2026-11-20T00:00:00+00:00    {"tenant_id": "lapsed-co", "status": "past_due", "state": "grace_expired", ...,
                                                  "hours_left": null, "hours_ago": 142}

States: `in_grace` (allowed), `grace_expired` and `past_due_no_grace` (denied), `canceled` (denied). A denied tenant also
gets 402 from the charges route, so use the no-edge command in step 3. Surprise: a tenant that is missing here and gets 402
has no `entitlements` row: step 8. Next: 3.

## 3. What was billed
    curl -s "$BASE/v1/tenants/$T/charges?start=2026-11-10T23:00:00Z&end=2026-11-11T00:00:00Z" -H "X-Tenant-Id: $T"
    {"tenant_id":"demo-1794359437", ... "lines":[{"model":"demo-model","hour_start":"2026-11-10T23:00:00Z","tokens_in":3000,
     "tokens_out":500,"price_version":1,"micros":6000}],"total_micros":6000,"unclosed_hours":[]}
One line per hour and model, rated at the price in force at `hour_start`: `(in*in_price + out*out_price + 500) // 1000` per line,
total = sum of lines (3000*1500 + 500*3000 = 6,000,000, /1000 = 6000). The total is complete only when `unclosed_hours` is `[]`:
it lists the hours in the range (oldest 100) that hold usage and have no closed rollup row, and those are not in the total:

    curl -s "$BASE/v1/tenants/$T/charges?start=2026-11-10T23:00:00Z&end=2026-11-11T03:00:00Z" -H "X-Tenant-Id: $T"
    {... "lines":[{"model":"demo-model","hour_start":"2026-11-10T23:00:00Z", ... "micros":6000}],"total_micros":6000,
     "unclosed_hours":["2026-11-11T01:00:00Z"]}

`start` and `end` are ISO 8601 text with a time zone (`start=5` is a 422 `must be ISO 8601 text with a time zone, such as
2026-11-01T00:00:00Z`) and may span at most 366 days (`the range is wider than 366 days: ask for the period in pieces`, 422).
Without the edge (a denied tenant), from the api container (one Python dict per line, then the total and the unclosed hours):

    docker compose exec -T api python -c "import sys; from datetime import datetime as d; from app import db, pricing; c = db.connect(); \
    a = [sys.argv[1]] + [d.fromisoformat(x) for x in sys.argv[2:]]; l, t = pricing.charge_lines(c, *a); [print(x) for x in l]; \
    print('total_micros', t, 'unclosed_hours', [h.isoformat() for h in pricing.unclosed_hours(c, *a)])" $T 2026-11-10T23:00:00+00:00 2026-11-11T03:00:00+00:00
    {'model': 'demo-model', 'hour_start': datetime.datetime(2026, 11, 10, 23, 0, tzinfo=datetime.timezone.utc), ... 'micros': 6000}
    total_micros 6000 unclosed_hours ['2026-11-11T01:00:00+00:00']

Surprises: `lines: []` means no closed rollup row in the period (hours still open, no events, or all late): `unclosed_hours`, then 5.
A non-empty `unclosed_hours` is a partial bill: an hour that just ended or is current is normal, one that stays is step 9 and 10.
409 refuses the whole range, never a partial bill; the detail names every unpriced model with its first unpriced hour, so add all
the prices in one pass (step 6); a single model reads `no price for model 'mystery-model' at 2026-11-10T22:00:00+00:00`:

    {"detail":"no price for 2 models: 'ghost-model' at 2026-11-10T22:00:00+00:00, 'mystery-model' at 2026-11-10T22:00:00+00:00"}
Next: 4.

## 4. Do the rollups match the raw log
    docker compose exec -T rollup python -m app.reconcile [--hour 2026-11-10T23:00:00Z | --since ISO] [--tolerance-bps 100] [--json] [--fail-on-unbilled]
    docker compose exec -T rollup python -m app.reconcile --hour 2026-11-10T23:00:00Z --actuals /dev/stdin < actuals.jsonl
Expect `drift=0` and exit 0. Above the summary line it always prints the usage no rollup will bill (information only; exit stays 0
unless `--fail-on-unbilled`, then any of these makes it 1; `--json` prints one object `{hours, rows, drift, not_billed}` instead):

    not billed: late events=2 tokens_in=800 tokens_out=200 (its hour was already closed when it arrived)
    not billed: unclosed events=0 tokens_in=0 tokens_out=0 (its hour ended over 2 hours ago and is still not closed)
    not billed: future events=0 tokens_in=0 tokens_out=0 (dated over 1 hour ahead)
    reconcile: hours=2 rows=4 drift=0
`actuals.jsonl` is the other source you compare against (the usage the customer or another system reports, which you write out yourself),
one object per line: `tenant_id, model, hour, tokens_in, tokens_out`. Exit 1 = drift, 2 = bad argument or file
(`--hour` must be a closed hour: `... is not a closed hour`). Drift lines (the summary reads `hours=1 rows=2 drift=1` and so on; the `not in closed_hours` line shows only without `--hour`
(`--since` or neither), since an hour not in `closed_hours` is exit 2 with `--hour`):

    DRIFT tenant=demo-... model=demo-model hour=2026-11-10T23:00:00Z tokens_in rollup=3007 events=3000
    DRIFT ... events but no rollup row (events=(3000, 500, 2))
    DRIFT tenant=ghost-co ... rollup row but no events (rollup=(10, 10, 1))
    DRIFT ... rollup row but its hour is not in closed_hours (rollup=(3000, 500, 2))
    DRIFT ... beyond 100 bps: tokens_in rollup=3000 actual=3500 diff=-500
    DRIFT ... rollup row but no actual (rollup=(3000, 500))      DRIFT ... actual but no rollup row (actual=(10, 10))
Different sums, a missing row, an extra row and a row whose `closed_hours` entry is gone (charges still bills it) mean the table
and the log disagree: the table was changed outside `app.rollup` or the rollup code is wrong (the close is one transaction, so a
failed close leaves no row). Do not edit it; ask whoever runs this service, and attach the `DRIFT` line and the reconcile output. A `beyond N bps`, `no actual` or `actual but no rollup row`
line compares with the other source: the usage the customer or Stripe saw was not all ingested, or was late (5).
Next: 5 if the numbers agree with the log but not the customer.

## 5. Accepted but not billed
    psql -v t="$T" <<'SQL'
    SELECT e.id, e.idempotency_key, e.tokens_in, e.tokens_out, e.occurred_at, e.received_at, c.closed_at
    FROM usage_events e JOIN closed_hours c ON c.hour_start = date_trunc('hour', e.occurred_at, 'UTC')
    WHERE e.tenant_id = :'t' AND e.late ORDER BY e.id;
    SQL
     4 | late-1 | 400 | 100 | 2026-11-10 23:45:00+00 | 2026-11-11 01:10:47+00 | 2026-11-11 01:10:38+00
A late event was stored but its hour was already closed when it arrived (`received_at` after `closed_at`); it is never in a
rollup, never billed, and no code reopens the hour. A client chooses `occurred_at`, so it can also choose a closed hour:
`reconcile` counts them (the `late` line, step 4). Per hour, billed against late:

    psql -v t="$T" <<'SQL'
    SELECT date_trunc('hour', occurred_at, 'UTC') AS hour, count(*) FILTER (WHERE NOT late) AS counted,
    count(*) FILTER (WHERE late) AS late FROM usage_events WHERE tenant_id = :'t' GROUP BY 1 ORDER BY 1;
    SQL
     2026-11-10 23:00:00+00 | 2 | 2
Events in an hour with no `closed_hours` row are simply not closed yet: the hour is due 5 minutes after it ends and the
`rollup` service runs every 3600 s. Open hours of the tenant:

    psql -v t="$T" <<'SQL'
    SELECT date_trunc('hour', e.occurred_at, 'UTC') AS hour, count(*) FROM usage_events e WHERE e.tenant_id = :'t'
    AND NOT EXISTS (SELECT 1 FROM closed_hours c WHERE c.hour_start = date_trunc('hour', e.occurred_at, 'UTC')) GROUP BY 1;
    SQL
     2026-11-11 01:00:00+00 | 1
Known cause: a closed hour is closed for every tenant, and any tenant's event in an old quiet hour makes the next rollup close
it, so another tenant's later event for that hour is late. Billing late events is a decision for the business, not a command.

## 6. Prices
    psql -c "SELECT model, version, effective_from, input_micros_per_1k, output_micros_per_1k FROM price_versions ORDER BY model, version"
     demo-model | 1 | 2020-01-01 00:00:00+00 | 1500 | 3000
The version used is the greatest `effective_from <= hour_start` (a tie goes to the later version), so a price that starts mid-hour
applies from the next hour. Compare with the `price_version` on the line; `pricing.price_at(conn, model, at)` returns it, or
raises `NoPrice: no price for model 'mystery-model' at ...` (never rated as zero). A missing price is added with
`app.pricing.add_price_version(conn, model, effective_from, in_per_1k, out_per_1k)`, which commits and returns the version number;
there is no CLI or API for it:

    docker compose exec -T api python -c "from datetime import datetime, timezone as z; from app import db, pricing; print(pricing.add_price_version(db.connect(), 'MODEL', datetime(2026,11,10,22,tzinfo=z.utc), 1500, 3000))"
    1
Fill in `MODEL`, the first hour the price applies from (UTC) and the input and output prices in micro-USD per 1000 tokens. A stored
version is never changed or removed: a trigger refuses it, but the trigger does not stop TRUNCATE, or the table owner or a superuser (who can disable the trigger). So check the values before you run it. A wrong version is superseded by a newer one, not edited: add
the corrected prices as the next version with the SAME first hour (a tie goes to the later version) and every hour from that first
hour on is rated at it, invoiced hours included (run: 1500/3000 corrected to 1000/2000 moved the hour's line from `price_version` 1,
6000 micros to `price_version` 2, 4000); a corrected version with a later first hour leaves the hours before it on the wrong version.
A version dated before invoiced hours changes what those hours would be rated at. For a 409 in step 3, add every model it names, then ask for the charges again:
in the run above the 409 `no price for model 'zz-model' at 2026-11-10T22:00:00+00:00` became a 200 with `price_version` 1.

## 7. A missing or doubled event
    psql -v t="$T" <<'SQL'
    SELECT id, idempotency_key, model, tokens_in, tokens_out, occurred_at, received_at, late FROM usage_events
    WHERE tenant_id = :'t' AND idempotency_key = 'late-1';
    SQL
`POST /v1/usage` answers 201 `accepted` (stored), 200 `duplicate` (same key and payload: nothing stored), 409 (`idempotency_key
was already used with a different payload`; nothing stored), 403 (`tenant_id does not match the entitled tenant`), 422 (invalid
body, or `occurred_at` not ISO text), 413 (body over 64 KiB: step 12), or an edge 401/402/503. The log
(`docker compose logs api | grep -E "usage (accepted|duplicate|conflict|rejected)|edge denied"`):

    INFO:     usage accepted: tenant=demo-... key='late-1' event_id=4 late=True       (a resend: `usage duplicate:` ...)
    WARNING:  usage conflict: tenant=demo-... key='late-1' reused with a different payload (event_id=4)
    WARNING:  usage rejected: body tenant 'lapsed-co' is not the entitled tenant 'demo-...'
    2026-11-11 01:11:02,586 WARNING app.edge edge denied: tenant='gone-co' status=402 reason=not entitled: status canceled
Missing: no row, so it was never accepted; ask the client for the status it saw. The hour is the UTC hour of `occurred_at`,
not `received_at`. Doubled: the service dedupes only on (tenant, key); two keys for one real call are two events. Look-alikes:

    psql -v t="$T" <<'SQL'
    SELECT model, tokens_in, tokens_out, occurred_at, array_agg(idempotency_key ORDER BY id) AS keys FROM usage_events
    WHERE tenant_id = :'t' GROUP BY 1,2,3,4 HAVING count(*) > 1;
    SQL
     demo-model | 400 | 100 | 2026-11-10 23:45:00+00 | {late-1,late-1-retry}
A doubled event is refused deletion by a trigger on UPDATE and DELETE; the trigger does not stop TRUNCATE, or the table owner or a superuser (who can disable the trigger): do not bypass it. There is no
adjustment feature: ask whoever runs this service for a manual credit, with the two keys from the query above.

## 8. Webhooks and grace
    psql -c "SELECT event_id, type, received_at FROM stripe_events ORDER BY received_at"
    psql -c "SELECT tenant_id, status, stripe_customer_id AS customer, payment_failed_at, grace_until, last_event_at FROM entitlements ORDER BY tenant_id"
A signed event is `processed` (state changed), `stale` (older than the last applied event for that tenant: ignored, but
recorded, with two exceptions: a delete of the tenant's stored subscription is applied whatever its age, and an older
`invoice.payment_failed` for a `past_due` tenant with no grace window opens one),
`duplicate` (event id already in `stripe_events`) or `ignored` (no tenant in the metadata, an unknown customer, an unhandled event
type, or a failed payment for a `canceled` tenant). The HTTP answer is `{"status": ...}`. `processed` and `stale` leave a
`stripe_events` row; a `duplicate` adds nothing and an `ignored` event is not recorded in the table, so read the outcome line
(INFO, `LOG_LEVEL` overrides; `tenant` is missing when none was found):

    docker compose logs api | grep -E "webhook (processed|stale|duplicate|ignored|refused|tie)"
    2026-11-11 01:10:47,923 INFO app.webhooks webhook duplicate: event='evt3_t2_fail1' type='invoice.payment_failed'
    2026-11-11 01:10:47,937 INFO app.webhooks webhook stale: event='evt3_t2_old' type='customer.subscription.updated' tenant='lapsed-co'
    2026-11-11 01:10:47,956 INFO app.webhooks webhook ignored: event='evt3_unknown' type='invoice.payment_failed' tenant=None
    2026-11-11 01:14:16,024 WARNING app.webhooks webhook tie after cancel: event='evt4_tie_late_active' type='customer.subscription.updated' tenant='tie-co' shares the second of the cancel and is stale
    2026-11-11 01:11:36,665 WARNING app.webhooks webhook refused: No signatures found matching the expected signature for payload
A subscription event stamped with the same second as a cancel is `stale` (a tie never undoes a cancel: the `tie after cancel`
line). Or use Stripe's own list of deliveries, which should show the response Stripe got (Stripe's side: not checked here, nothing in this repo calls Stripe). Refusals: 400 `missing Stripe-Signature header` (no header), 400 `invalid signature or payload` (bad signature),
413 `webhook body too large` (over 1 MiB), 503 `database unavailable` (database down; step 11), 503 `webhook secret not configured`
(no or blank `STRIPE_WEBHOOK_SECRET`) or 503 `grace period not configured` (`GRACE_PERIOD_HOURS` out of range). A payment failure
opens grace for `GRACE_PERIOD_HOURS` (default 72, 0 to 8760) from the FIRST failure; later retries do not extend it; an `active`
event clears it; a cancel clears it and nothing but a subscription event reopens access.

## 9. The bill total looks too low
    curl -s "$BASE/v1/tenants/$T/charges?start=2026-11-10T19:00:00Z&end=2026-11-10T21:00:00Z" -H "X-Tenant-Id: $T"
    {... "lines":[{"model":"demo-model","hour_start":"2026-11-10T19:00:00Z","tokens_in":500, ... "micros":765}],"total_micros":765,
     "unclosed_hours":["2026-11-10T20:00:00Z"]}
Read `unclosed_hours`. `[]`: the bill holds every closed hour; the gap is late events (step 5), a price (step 6) or a missing
event (step 7). Not empty: the total is partial. An hour that is current or ended less than about 65 minutes ago is normal (due
5 minutes after it ends, then the hourly loop): ask again later. An older hour that stays in the list is one the rollup cannot
close: `docker compose logs rollup | grep "rollup failed"`, then step 10. `reconcile` shows the same usage on its `unclosed` line once the hour ended over 2 hours ago.

## 10. One hour will not close
    docker compose logs rollup | grep "rollup failed"
    docker compose exec -T rollup python -m app.rollup ; echo "exit=$?"
    2026-11-11 01:12:12,947 ERROR __main__ rollup failed: hour=2026-11-10T20:00:00+00:00 error=NumericValueOutOfRange('bigint out of range')
    rollup: closed 0 hours, failed 1: 2026-11-10T20:00:00+00:00
    exit=1
A failed hour is rolled back (no `closed_hours` row, no rollup rows), the hours after it still close (also when the
failure was a lost database connection: the next hour gets a new one), the loop logs it again every
cycle and a one-shot run exits 1. The line carries the hour and the database error (some errors quote the failing row, so treat the
log as tenant data). Find who is in that hour (a sum past 9.2e18 tokens, from events the client dated into it):

    psql -c "SELECT tenant_id, model, count(*) AS events, sum(tokens_in) AS tokens_in, sum(tokens_out) AS tokens_out FROM usage_events
             WHERE date_trunc('hour', occurred_at, 'UTC') = '2026-11-10T20:00:00Z' AND NOT late GROUP BY 1, 2 ORDER BY 4 DESC"
     demo-1794359437 | demo-model | 9223373 | 9223373000000000000 | 0
(illustrative: `tokens_in` is capped at 10^12 per event, so a sum past 9.2e18 takes about 9.2 million events in one hour)
What to do: read `error=`. `bigint out of range`: no command closes it. The events are refused deletion by the append-only trigger; the trigger does not stop TRUNCATE, or the table owner or a superuser (who can disable the trigger): do not bypass it. The columns
stay BIGINT until a developer widens them (docs/ingest.md, Known limit): ask whoever runs this service, with the `rollup failed` line and this output; until then the hour
is not billed and stays in `unclosed_hours`. Any other error: fix the cause it names (the one reproduced here was a CHECK constraint added by hand
on `usage_rollups_hourly` and then dropped), then run `python -m app.rollup` again (above) or wait one cycle; the hour closes
with the same numbers and `closed hour=...` prints:

    closed hour=2026-11-10T19:00:00+00:00 rows=1 events=1
    rollup: closed 1 hours, failed 1: 2026-11-10T20:00:00+00:00      (exit 1 while any hour still fails)

## 11. Clients get 503, some with Retry-After
    curl -s -i "$BASE/v1/tenants/$T/charges?start=2026-11-10T23:00:00Z&end=2026-11-11T00:00:00Z" -H "X-Tenant-Id: $T"
    docker compose ps -a db ; docker compose logs api | grep -E "edge cannot read|unavailable"
Two different 503s. `{"detail":"entitlement check unavailable"}` (no `Retry-After`) comes from the edge: the API cannot read the
database. The log says why: `edge cannot read entitlements: failed to resolve host 'db'` (db stopped) or `... connection timeout
expired` (db not answering; a new connection gives up after 5 s, measured with a paused db: 5.04 s with no load, 7.9 s with 50 clients
retrying, because they queue for a worker thread; a stopped db answered in 4.0 s; a measurement from a walk of this runbook on 5dca15d, not a guarantee). Start or fix the db; the API needs
no restart, and `/healthz` keeps answering 200 meanwhile: it shows the process is up and its event loop answers, not the
database, so it does not tell you the db is up.
`{"detail":"database unavailable"}` with header `retry-after: 1` comes after the edge passed: a lock wait over 2 s, a statement over
15 s (`app/db.py`; not run here), or the db dying mid-request. Nothing of the failed attempt was stored, so the same request can be
resent. A held lock shows in the log and in the database (the usage request waited 2.02 s):

    ERROR:    usage unavailable: canceling statement due to lock timeout
    psql -c "SELECT l.pid, l.mode, l.granted, l.objid AS hour_index, left(a.query, 60) AS query, now() - a.xact_start AS open_for
             FROM pg_locks l JOIN pg_stat_activity a USING (pid) WHERE l.locktype = 'advisory'"
     106 | ExclusiveLock | t | 498431 | BEGIN; SELECT pg_advisory_xact_lock(1, 498431); SELECT pg_sl | 00:00:02.000321
`objid` is the hour as whole hours since the epoch (498431 = 2026-11-10T23:00Z). The rollup closing a large hour holds that lock
and ingest into the same hour waits for it: wait for the close, or look at `docker compose logs rollup`. Ending a stuck
session (`SELECT pg_terminate_backend(pid)`) rolls its transaction back; a close ended that way leaves the hour open and the next
run closes it with the same numbers (`tests/test_attack_ingest.py`; not run here). The 503s for a missing webhook secret or grace
setting have no `Retry-After` either (by reading `app/webhooks.py`).

**No answer at all** (clients time out, no 503) while `/healthz` is 200: the database may be frozen, not stopped.
`docker compose ps -a db` then shows `Up ... (Paused)` (run). Each request already inside the database waits for a server
that is not running, so no timeout fires, and 40 of them take every worker thread; a new request that needs a thread waits too.
Keepalive options do not end it (measured, `docs/ingest.md`). Fix: `docker compose unpause db`; the parked requests answer 503
within seconds and can be resent. Or `docker compose restart api` (about 5 s): the parked requests are dropped, and new ones get
503 `entitlement check unavailable` after about 5 s (7.9 s with 50 clients retrying, walk on 5dca15d) until the db is back. Both measured with a paused db and 50 requests in flight.

## 12. 413 on /v1/usage
    python3 -c "print('x'*70000)" | curl -s -i -X POST $BASE/v1/usage -H 'Content-Type: application/json' --data-binary @- | sed -n '1p;$p'
    HTTP/1.1 413 Request Entity Too Large
    {"detail":"request body too large"}
`/v1/usage` refuses a body over 64 KiB (65,536 bytes is read as usual, 65,537 is a 413): it rejects an oversized
`Content-Length` immediately, or counts streamed chunks and answers 413 as soon as they pass the cap, without buffering
the rest. This happens before any credential check, so a caller with no `X-Tenant-Id` gets 413 too. An event is a few
hundred bytes, so a 413 is a client sending the wrong thing or a batch: look at the client, not the service. The access
line is `"POST /v1/usage HTTP/1.1" 413` in
`docker compose logs api`. The webhook has its own limit (1 MiB, `webhook body too large`, step 8).

## Symptoms at a glance
| Symptom | Most likely cause | Look at |
|--|--|--|
| Total is 0 or `lines: []` | hour not closed yet (`unclosed_hours`), events all late, or no events | 3, 5 |
| `unclosed_hours` not empty, total too low | current or just-ended hour, or an hour the rollup cannot close | 9, 10 |
| `rollup failed` in the rollup log | a sum past BIGINT, or a database error in that hour | 10 |
| 409 `no price for ... model(s)` on charges | a model with no price version in force at that hour | 6 |
| 422 on charges | `start` or `end` not ISO text with a time zone, or a range over 366 days | 3 |
| Customer says usage is higher than billed | late events, or events in an open hour | 5, then 4 with `--actuals` |
| Customer says usage is lower than billed | two keys for one call, or the wrong price version | 7, 6 |
| Billed amount differs by 1 micro | per-line half-up rounding, summed once per line | 3 |
| 503 `entitlement check unavailable` | database stopped or not answering | 11 |
| 503 `database unavailable`, `retry-after: 1` | a lock wait over 2 s, a statement over 15 s, or the db dying | 11 |
| No answer at all, `/healthz` still 200 | the db container is paused (frozen), not stopped | 11 |
| 413 on `/v1/usage` | request body over 64 KiB | 12 |
| 402 on usage or charges | no entitlement row, canceled, or grace expired | 2, 8 |
| `reconcile` exits 1 | drift (rollup table changed outside `app.rollup`, or the other source differs), or `--fail-on-unbilled` | 4 |
| Tenant paid but still denied | only a `customer.subscription.*` event reopens access; `invoice.paid` does not | 8 |
