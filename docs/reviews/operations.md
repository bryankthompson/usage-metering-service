# Red team, lens: operations (checkout of origin/main da520b6)

> Review report as written at the time. File:line citations refer to the SHA named in the report, not to today's tree.

Ran against my own compose project (rt-operations, API 18391, DB 15391), torn down with `down -v`. No runbook exists on this checkout (docs/runbook.md absent).
Summary: 0 blockers, 2 major, 4 minor, 2 notes.

## RT-operations-1  MAJOR  A paused or unreachable database hangs every request, with no timeout, and starves /healthz
Files: app/db.py:17 (`psycopg.connect(url)` with no `connect_timeout`), no `statement_timeout` anywhere; app/main.py:42 (healthz).
Only a database that refuses or resets connections gives the documented 503. A database that stops answering (frozen host, network
partition, `docker pause`) gives no error: connect and queries wait forever, each request holds a worker thread, and the default thread
pool (40) fills, so even `/healthz` (which never touches the DB) stops answering. The 503 path the docs promise is never reached.
Repro:
    docker compose pause db
    curl -m 20 -X POST localhost:18391/v1/usage -H 'X-Tenant-Id: good' -H 'content-type: application/json' -d '{...valid event...}'
      -> usage during paused db: 000 20.000920s   (no answer at all)
    (110 more requests in the background, then)  curl -m 5 localhost:18391/healthz -> 000 after 5.00s
    docker compose unpause db  -> healthz 200 again (it does recover)
Smallest fix: `connect(url, connect_timeout=5, options="-c statement_timeout=15000")` in db.connect, so the existing OperationalError->503 handler fires.

## RT-operations-2  MAJOR  A closed-hour failure leaves later hours unclosed, and charges answers 200 with total 0 for them
Files: app/rollup.py:97-100 (loop over due hours, no per-hour isolation), app/rollup.py:117-122, app/pricing.py:113-120.
docs say the BIGINT overflow stops closing later hours "loudly". The loudness is a traceback with no hour, tenant or model in it,
and the billing side is quiet: `GET /v1/tenants/{t}/charges` reads only closed rollups, so for an unclosed hour it returns
`200 {"lines":[],"total_micros":0}` and logs `charges computed ... lines=0 total_micros=0`. A bill for a month with a blocked hour is
silently short. Any other poison (not just overflow) behaves the same: one hour that always fails blocks every later hour.
Repro (poison hour via direct insert of 9.3M rows of 10^12 tokens, plus one good event at 05:30 the same day):
    docker compose logs rollup | tail  ->  psycopg.errors.NumericValueOutOfRange: bigint out of range   (no hour named)
    docker inspect rt-operations-rollup-1 -> restarts=9 status=restarting health=none   (9 restarts in ~70 s, grows forever)
    curl -H 'X-Tenant-Id: good' '.../v1/tenants/good/charges?start=2020-01-01T00:00:00Z&end=2020-01-02T00:00:00Z'
      -> {"tenant_id":"good",...,"lines":[],"total_micros":0}      (the good 05:30 event is not billed)
Smallest fix: in close_due_hours wrap each close_hour in try/except, log `rollup failed hour=%s` with the error, continue with the next
hour, and exit non-zero at the end; and have charges refuse (or flag) a range that contains an hour with events but no closed_hours row.

## RT-operations-3  MINOR  The advised recovery for an old volume deletes the data, though a repeatable migration exists
Files: docs/demo.md:29, README.md:89, app/db.py:21.
Compose never runs db/*.sql on an existing volume. Dropping a column from 002 simulates an old volume: the webhook is then a bare 500
(`KeyError: 'last_event_at'` in the api log, at app/webhooks.py:Unknown row access) and the docs tell the operator to `down -v`, which deletes the
usage log. `apply_schema` is idempotent and is already inside the image (Dockerfile copies db/). Proof that it repairs a live stack:
    docker compose exec -T api python -c "from app import db; c=db.connect(); db.apply_schema(c); print('applied')"  -> applied
    (column last_event_at is back, nothing lost)
Fix: document that command as the upgrade step (or run it at API/rollup start); never recommend `down -v` for an upgrade.

## RT-operations-4  MINOR  Rollup failure message has no context; no health signal on rollup or api
Files: app/rollup.py:93-100, compose.yaml:22-42.
The rollup crashes with a bare driver traceback: no hour, no tenant. Neither `api` nor `rollup` has a compose `healthcheck`, and `api`
`/healthz` does not check the DB (it answered 200 while every route returned 503 with the entitlements table missing). `docker compose ps`
shows `Restarting (1)` for the poisoned rollup, but nothing a monitor can poll.

> Note, 2026-11-11: since this report, compose.yaml gives `api` a healthcheck (compose.yaml:34-38); `rollup` still has none.
Fix: log `rollup: closing hour=... failed` before re-raising; make /healthz run `SELECT 1` and add healthchecks.

## RT-operations-5  MINOR  charges has no range or result bound
File: app/pricing.py:113-120 (all rows loaded, then the whole list serialised).
One tenant with 500,001 hourly rows (inserted directly) and a 1990..2100 range: 55 MB response in ~2-7 s; 8 concurrent calls took the
api container to 1.7 GiB (`docker stats`: 985 MiB, 1.44, 1.70 GiB). Realistic tenants are far smaller; the risk is a wide range from a
caller. Fix: cap `end - start` (for example 366 days) with a 422, or paginate.

## RT-operations-6  MINOR  DUE_HOURS scans the whole usage_events table every run
File: app/rollup.py:42-48. `date_trunc(...) + interval <= cutoff` is not index-friendly. With 9.3M rows the query takes 1.96 s
(`EXPLAIN ANALYZE` Execution Time: 1961.913 ms, seq/index-only scan of everything) and it grows linearly with history, every hour, forever.
Fix: restrict to `occurred_at < cutoff - 1 hour` and `occurred_at >= (select max(hour_start) from closed_hours) - something`, or start from the oldest unclosed hour.
> Note, 2026-11-11: fixed differently: `DUE_HOURS` is now a recursive CTE that walks only the hours that have events, one `(occurred_at)` index probe per hour (27 ms against 647 ms on 1.2M events), so the cost grows with hours, not events; see "Query cost" in docs/ingest.md.

## RT-operations-7  NOTE  Events retried after their hour closed are unbilled, with a 201
The close runs 5 min to ~65 min after the hour ends (the loop sleeps 3600 s from the end of the previous run, not on the hour). A
client that buffers through a 70-minute outage and resends gets 201 `"late": true` and is never billed. The docs state "late is not
billed"; they do not say how long a client has before that happens. Worth one line.

## RT-operations-8  NOTE  scripts/test publishes the shared test DB on 0.0.0.0:5455
`docker ps` shows `metering-test-db 0.0.0.0:5455->5432`. The script warns about it, but the container still exists in that state here.
Credentials metering/metering, test data only.

## What I tried that held
- scripts/outage-check.sh against a live stack: passes (201, 503 during stop, 201 after, resend 200, 2 rows).
- Webhook while the DB is stopped: 503 `database unavailable` (took 4.1 s, DNS failure), no 500; event not recorded, so Stripe retries are safe.
- pg_terminate_backend loop (40 kills over 12 s) during flood.py 600/100: answers 201/200/503 only (299/235/66), 0 tracebacks, no 500.
- flood.py 2000 requests / 400 clients: exactly 1000x201 + 1000x200, 0 tracebacks, 100 max_connections never exhausted.
- Missing entitlements table: every route 503 with a clear ERROR line naming the relation (logged once per request plus one WARNING).
- DB SIGKILL (`docker compose kill -s KILL db`) then `up -d`: data intact, healthy in ~10 s. (Note: `docker compose kill` is a manual stop for the restart
  policy, so neither db nor api came back by themselves; that is Docker semantics, not a finding.)
- Rollup is atomic: a failed close leaves no closed_hours row and no rollup rows (the poisoned hour kept retrying cleanly).
- Compose start order: api and rollup wait for db healthy.

## What I did not get to
Clock jumps on the rollup host, a kill -9 of the rollup process exactly inside close_hour, two rollup processes together, runbook (does not exist),
EXPLAIN of the payment_failed lookup by stripe_customer_id (no index on that column in db/002_billing.sql, expected seq scan on a large entitlements table), log noise at INFO per request beyond noting that
3000 requests wrote ~5400 api log lines (one app line plus one access line per request).

# Round two (checkout: origin/main 1abb7b4, live stack, own project and ports)

## Status of my round-one repros
| Round-1 finding | Status | New output |
|---|---|---|
| RT-operations-1 paused DB, before connect | FIXED | request returns 503 at 5.0 s, /healthz 200 |
| RT-operations-1 paused DB, mid-connection | STILL FAILS | see RT-operations-9 |
| RT-operations-2 poisoned hour blocks later hours, charges silently 0 | FIXED | one `rollup failed: hour=... error=...` ERROR line plus one summary line per cycle (no spin); later hours close; one-shot exits 1; no advisory lock or connection left over; charges lists `unclosed_hours` |
| RT-operations-3 `down -v` advice | CHANGED | apply_schema is idempotent now, but see RT-operations-10 (data loss by recreate) |
| RT-operations-5 charges unbounded | FIXED for range (366-day cap gives 422); response size still unbounded, see RT-operations-11 |
| Statement timeout 15 s vs legitimate work | HELD | 114 models x 8784 hours (about 1M rows, 366 days): 200 in 3.6 s, so the timeout cancels nothing legitimate |
| Reconcile orphan / not-billed SQL at 1M events / 2000 hours | HELD | about 1.1-1.5 s, drift=0, exit 0 |
| scripts/demo.sh, scripts/outage-check.sh | HELD | both pass on main |

## Runbook (docs/runbook.md, run literally on main)
Sections 2-8 ran as written (charges curl, entitlements, `app.delinquent` with `--json`/`--fail-on-expired`/`--at`, `app.reconcile --hour` with and without `--actuals`,
late-event and open-hour SQL, price_versions, duplicate-payload SQL, stripe_events SQL, the `docker compose logs api | grep` lines): no command failed and no output contradicted the text.
`reconcile --hour` on a non-closed hour exits 2 with a clear message. Charges with `start=5` gives a clear 422. The "charge_lines" snippet in section 3 I ran in my own form (python -c), not copied verbatim.

## New findings

### RT-operations-9 MAJOR, a frozen DB mid-query still hangs the API (fix is partial)
app/db.py: connect_timeout=5 and statement_timeout are server/connect-phase only; there is no client-side `tcp_user_timeout` or keepalive. When Postgres is frozen
(`docker pause`) after a connection is open, a request waiting inside a query gets no answer in 30 s; with 50 in-flight requests /healthz also returned 000 (no answer). It recovers after unpause.
Smallest fix: add `keepalives=1 keepalives_idle=5 keepalives_interval=2 keepalives_count=3 tcp_user_timeout=15000` to the conninfo (server-side timeouts cannot fire on a frozen server).

### RT-operations-10 MAJOR, the database data lives in an anonymous volume; recreating the db container loses it
compose.yaml has no named volume for Postgres. Repro: `docker compose run --rm ...` after a config change (port) recreated db; price_versions count went 1 -> 0. `up -d` keeps data.
Smallest fix: a named volume `pgdata:/var/lib/postgresql/data`.

### RT-operations-11 MINOR, charges response size is unbounded within the 366-day cap
1M rows over 366 days returned a 114 MB body in 3.6 s (memory in the api and the client). Fix: cap the line count (422 or pagination), or document it.

### RT-operations-12 MINOR, UNPROVEN: rollup/reconcile/delinquent have statement_timeout 0 and no client timeout
A frozen DB would hang `rollup --every` silently (no log, no exit). I did not test it. Same fix as RT-operations-9 plus a log line per cycle start.

### RT-operations-13 NOTE, a failing close holds the exclusive hour lock for the whole attempt, every cycle
close_hour takes the lock first. Ingest into the poisoned hour sees intermittent 503 after 2.0 s (lock timeout); retryable and visible, so a NOTE.

## What held (round two)
Spin-logging: none. Hidden permanent failure: not hidden (ERROR each cycle, exit 1 for one-shot, `unclosed_hours` in charges). Locks/connections after a failed hour: none left. Statement timeout: no legitimate query cancelled. Reconcile at 1M rows: fast.

## Not reached
Clock jumps; kill -9 inside close_hour; two rollup processes together; frozen DB against `rollup --every` (RT-operations-12).
