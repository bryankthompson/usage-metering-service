# Usage intake: `POST /v1/usage`

Stores one usage event in the `usage_events` table (`db/001_usage.sql`) behind the edge check `require_entitlement`, which
reads the entitlement on its own connection (the handler uses a second one). A trigger refuses UPDATE and DELETE on the table;
the trigger does not stop TRUNCATE, or the table owner or a superuser (who can disable the trigger). In compose the
application connects as that superuser, so the trigger guards against mistakes, not against the service's own credentials.

Body (all required): `tenant_id`, `idempotency_key` and `model` (1 to 200 chars each, no NUL character), `tokens_in` and `tokens_out` (integers 0 to 10^12), `occurred_at` (ISO 8601 text with a time zone, UTC year 1 to 9999; a number is refused, not read as Unix seconds). The 200-char cap keeps these columns inside Postgres's index key limit: an oversized `model` would be stored and then stop the rollup from closing any later hour.

| Status | When |
|--|--|
| 201 `{"status": "accepted", "event_id": N, "late": false}` | new event stored |
| 200 `{"status": "duplicate", "event_id": N, "late": false}` | same key and payload as a stored event; nothing stored |
| 409 | same key, different `model`, tokens or `occurred_at`; nothing stored |
| 403 | body `tenant_id` is not the tenant the edge admitted |
| 401 / 402 / 503 | the edge refused, nothing stored: no `X-Tenant-Id` header / no usable entitlement / entitlement check failed (see `docs/billing.md`) |
| 503 | the database went away after the edge check: nothing is confirmed stored, and resending the same key is safe |
| 503 + `Retry-After: 1` | the request waited 2 seconds (`db.LOCK_WAIT`) for a lock, usually the hour's lock while that hour is being closed: nothing is stored, resend the same key (a resend after the hour is closed is stored as late and not billed) |
| 413 | the body is over 64 KiB (an event is a few hundred bytes). Refused from `Content-Length`, or while the stream arrives if there is none, before the body is parsed and before the edge runs, so an oversize body gets 413 whether or not it has credentials |
| 422 | invalid body (naive or numeric timestamp, text too long or with a NUL, a count out of range). The body lists `type`, `loc` and `msg` only, never the input: echoing it made a `NaN` or a lone surrogate a 500 |

## How it stays correct

- **Resends:** one `INSERT ... ON CONFLICT (tenant_id, idempotency_key) DO NOTHING RETURNING id, late`.
  Two identical requests at once insert once; the loser gets no row back, reads the stored event and
  compares payloads. There is no check-then-insert.
- **Late events:** the same INSERT sets `late = true` when the event's UTC hour is already in
  `closed_hours` (`date_trunc('hour', occurred_at, 'UTC')`, so the session time zone cannot move
  the hour). A late event is answered 201 with `"late": true` (a resend says so too, as does the log
  line), is stored, and is never in a rollup, so it is **not billed**. Ingest first takes a shared lock
  for the event's hour (see Rollups). **How long a client has:** an event is on time until its hour is closed. With the
  hourly loop the close runs between 5 minutes (the grace) and about 65 minutes (grace plus one loop of 3600 s) after
  the hour ends, so a client that buffers through an outage and resends later than that gets `201` with `"late": true`
  and is never billed for that event.

**Tests:** `docker compose up -d db`, then `uv run pytest -q tests/test_ingest.py tests/test_attack_ingest.py` (`DATABASE_URL` if not port 5432). The second file is the hostile-input, race and outage tests.

**Does not do:** batch intake, or authentication beyond the edge dependency.

# Rollups (`app/rollup.py`)

`close_hour(conn, hour_start)` sums the non-late events of one UTC hour per (tenant, model) into
`usage_rollups_hourly` and inserts the `closed_hours` row, in one transaction. A second close of the
same hour changes nothing and returns `None`. `hour_start` must be a timezone-aware UTC hour boundary
(else `ValueError`).

`close_due_hours(conn, now, grace=5 min)` closes, oldest first, every unclosed hour that has events
and ended at least `grace` before `now`. An hour with no events is not closed.

**Never both, never neither.** Each event of a closed hour is in its closed rollup or flagged `late`. Ingest
takes the shared advisory lock `pg_advisory_xact_lock_shared(1, hour)` before its INSERT and holds it
to commit; `close_hour` takes the exclusive lock for the hour before it reads anything.
- An ingest already in flight finishes first, so the close counts its event (`late = false`).
- An ingest that arrives during the close waits; its INSERT then sees the hour in `closed_hours` and
  stores the event with `late = true`. A late event, a resend (200) or a conflict (409) never
  changes a closed rollup row.

**CLI:** `python -m app.rollup` closes the due hours once; `--every SECONDS` repeats. It prints
`closed hour=... rows=N events=N` per closed hour, or `nothing due`. A close runs with no statement timeout.
Compose runs it as the `rollup` service (`--every 3600`).

**Tests:** `uv run pytest -q tests/test_rollup.py tests/test_due_hours.py` (the second compares `DUE_HOURS` with the old query and guards its plan). The lock tests hold one side's lock by hand and
check the other side waits.

**Does not do:** reconciliation, pushing usage to Stripe, a query API over rollups, a queue.

**Known limit:** the rollup token columns are BIGINT. If one tenant's events for one model in one UTC hour
(taken from the client's `occurred_at`, so not necessarily the current hour) sum past 9.2e18 tokens, which is
9,223,373 events at the 10^12 per-event cap, closing that hour fails with `bigint out of range` and rolls back (no `closed_hours` row, no rollup rows).
The hour stays unclosed and is retried every run until the columns are widened to NUMERIC.

**A failed hour** is logged at ERROR as `rollup failed: hour=<UTC hour> error=...` and the later hours still close.
`error=` is the database's own error text, so the line can carry tenant data: a CHECK violation printed
`DETAIL:  Failing row contains (<tenant>, <model>, ...)` (run), while the BIGINT overflow prints only `bigint out of range`.
Treat the rollup log as tenant data.
The one-shot run prints `rollup: closed N hours, failed M: <hours>` and exits 1; with `--every` the loop keeps sleeping and logs it every cycle.
Find it with `docker compose logs rollup | grep "rollup failed"`. Hours may close out of order; that never makes an event late.

**Known limit: one very large hour, or one that fails to close.** Closing an hour holds its exclusive lock for the whole
close, and the close has no lock timeout by design. The red team measured a 12M-row hour: the close took 23.8 s, and 160 of the 1,226 ingests for THAT
hour (13 %) waited past the 2 s lock wait and answered 503 with `Retry-After: 1`; ingests for another hour in the same run:
1,979 accepted, 0 refused. With a 4M-row hour (the run took 3.4 s) none was refused (these figures are the red team's, not
repeated here). A refused ingest stores nothing, and a resend during the close waits and is refused again. A resend after the
close is accepted (201) but stored as `"late": true`, because the hour is closed by then, so it is never billed (run: hold the
hour's lock, ingest gives 503, release, `close_hour`, resend gives 201 with `late: true` and the event is in no rollup row). A
client that does not resend loses the event as well. A close that fails holds the same exclusive lock for the whole failed
attempt, on every cycle (`close_hour` takes the lock first), so ingests for that hour see an intermittent 503 after 2 s
(red team, RT-operations-13: retryable and visible; the reports are in [docs/reviews/](reviews/README.md)). Not tested: an ingest transaction left `idle in transaction` while it
holds the shared hour lock would block that hour's close for as long as it stays open (the closing side has no timeout:
`test_the_closing_side_has_no_timeout_it_waits_for_ingests_in_flight`), and no `idle_in_transaction_session_timeout` is set.

**Query cost (measured 2026-11-11, Postgres 16 in a docker container on a WSL2 laptop; times move with the machine, the plans and row counts do not).**
- **Which hours are due (`DUE_HOURS`).** The old query wrapped `occurred_at` in `date_trunc` in both predicates, so no index could serve it:
  on 1.2M events over 3,001 hours (6 of them open) it was a Seq Scan reading all 1.2M rows, 647 ms. The review's proposed extra
  `AND occurred_at < cutoff` did not help: nearly every event is older than the cutoff, the plan stayed the same Seq Scan, 655 ms. The
  query is now a recursive CTE that walks the hours that have events: from one hour, the first event at or after the next hour boundary
  (one probe of the `(occurred_at)` index) names the next hour, and each hour is checked against `closed_hours`. Same plan on the
  same data: 3,001 index probes, 27 ms (24 times faster), the same 6 hours. The cost grows with the number of hours that have events
  (closed ones included, because an unclosed hour can be anywhere in the past), not with the number of events. A test compares it with the old query over
  hour boundaries, the cutoff +/- 1 microsecond, non-UTC offsets, closed and out-of-order hours, late events, two tenants, year 1 and
  year 9999, and another asserts the plan reads at most about one event per hour (`tests/test_due_hours.py`).
  A row with `occurred_at` `infinity` or `-infinity` (the schema allows it, the API refuses it) cannot make the walk loop: every step must move forward.
  `infinity` is ignored, as before; `-infinity` is still returned as a due hour, as before, and `close_due_hours` then stops with a psycopg `DataError`
  (the driver cannot load it as a datetime), a loud failure, not a hang.
- **Unclosed hours on a charges call (`UNCLOSED_HOURS`, `app/pricing.py`).** The index `usage_events_tenant_occurred_at` on `(tenant_id, occurred_at)`
  serves its `tenant_id = X AND occurred_at in range` filter. Same data, 20 tenants, warm cache: a 30-day range took 33 ms before (an
  `(occurred_at)` index scan that filtered 298,000 rows) and 13 ms after (a bitmap scan of that tenant's rows). A 1-day range took 1.3 ms
  both ways, so the index pays when the range is wide or there are many tenants, not for a short range. The gain grows with the number of tenants; it was measured with 20.
- **What the index costs.** One more btree write on every ingest. 10,000 single-row inserts into a table of 1.2M rows, three rounds each, median:
  209 microseconds with the index and 189 without (+20 microseconds, about 10 %) with `synchronous_commit = off`, which isolates the CPU
  cost. With the default `synchronous_commit = on` each insert waits for a disk flush (about 7 ms here) and the run-to-run spread
  (50 to 86 s per 10,000) was larger than the difference (74.1 s with, 69.2 s without), so that run shows no cost that can be told apart from the noise.
- **The `(occurred_at)` index stays.** Both queries use it: `AGGREGATE_HOUR` on one 400-event hour of that data is an Index Scan using
  `usage_events_occurred_at` (0.18 ms), and the new `DUE_HOURS` probes it once per hour.
- **Building the index on an existing table.** `apply_schema` runs every `db/*.sql` in one transaction, so the statement is
  `CREATE INDEX IF NOT EXISTS`, not `CONCURRENTLY`. On a database that already holds a large `usage_events`, the first start after this change takes
  a write lock on that table while the index builds, so ingests wait (a 2 s lock wait then answers 503, see the timeouts below) until it
  finishes. Applying the schema again afterwards does nothing. Not measured: the build time on a large table.

**Timeouts:** every connection gives up connecting after 5 s; request-path connections cancel a statement after 15 s,
so a database that stops accepting connections, or answers slowly, is a 503. `/healthz` is an `async` route that touches
nothing, so it keeps answering while every worker thread is busy; it shows that the process is up and its event loop
answers, not that the database does.
Known limit: a database that freezes after a connection was made (a paused container) is not bounded, because the
statement timeout is enforced by the server and the client has no timer of its own; the settings query right after
connecting is likewise not bounded (found by review, not fixed).
Measured on a frozen server (`docker pause`) after a connection was open: a request waiting inside a query got no answer in
30 s (red team, RT-operations-9), and a plain `SELECT 1` on an already-open connection was still blocked after 22 s, past the
15 s statement timeout (run). `/healthz` used to stop answering with 50 requests in flight because it shared the worker pool;
since it is `async` it does not (live, db paused with 50 requests parked: no answer in 3 s before, 200 in 2 ms after).
**TCP keepalive does not end the wait (run).** The red team's proposed options, `keepalives=1 keepalives_idle=5
keepalives_interval=2 keepalives_count=3 tcp_user_timeout=15000` in `DATABASE_URL` (psycopg reported `tcp_user_timeout` 15000 on
the connection), left 50 requests parked in a lock wait on a paused db still waiting at 16, 30, 45 and 65 s of an 80 s
freeze. The kernel of a frozen container keeps acknowledging and answering probes, so the options help only against a dead host
or a cut network. They are not set. (The requests in this run were in a lock wait, not a running query; the red team's run with a query
hung the same way.)
**While frozen:** each parked request holds a worker thread; 40 of them fill the pool, and then a new request that needs a
thread gets no answer (nothing in 20 s, run) while `/healthz` answers. With only a few parked, a new request is a 503
`entitlement check unavailable` after 5 s (log `edge cannot read entitlements: connection timeout expired`; run on POST
`/v1/usage` and charges).
**Recovery:** `docker compose unpause db`: all 50 parked requests answered 503 within 4 s (the server's timers fire on
resume; a refused request stores nothing, so resend). Or `docker compose restart api` (healthz back in about 5 s): the parked
requests are dropped with no answer (the client sees a closed connection) and new ones get a 503 after 5 s until the db is back.
For `rollup --every` the rollup has no statement timeout (`statement_timeout_ms=0`). Run: a freeze that starts between two
cycles ends the process, because the next connect gives up after 5 s (`ConnectionTimeout`, a traceback, exit 1; the compose
`rollup` service has `restart: unless-stopped`). Not run: a freeze that starts while the rollup is inside a query; by the
code nothing bounds that wait (RT-operations-12 was reported unproven).
