# Usage intake: `POST /v1/usage`

Stores one usage event in the `usage_events` table (`db/001_usage.sql`) behind the edge check `require_entitlement`, which
reads the entitlement on its own short checkout from the connection pool (the handler takes a second, short one; nothing is held in between). A trigger refuses UPDATE and DELETE on the table;
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
| 504 `{"detail": "query timed out"}`, no `Retry-After` | a statement ran past the 15 s statement timeout (the database is up, so it is not a 503): nothing is stored, and the same query would time out again |
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

**Tests:** `scripts/test tests/test_ingest.py tests/test_attack_ingest.py tests/test_ingest_batch.py` (own database on port 5455). The second file is the hostile-input, race and outage tests; the third is the batch route (below).

**Does not do:** authentication beyond the edge dependency.

## Batch intake: `POST /v1/usage/batch`

Stores up to 1000 events in one request and one transaction, behind the same edge check. Body: `{"events": [<event>, ...]}`, each
event exactly as for `POST /v1/usage`. **All or nothing:** if any event is refused, or the transaction fails, nothing of the
batch is stored, so a resend of the whole batch is always safe. Each event is deduplicated by the single route's rule
(`(tenant_id, idempotency_key)`, same payload is a duplicate, different payload is a conflict), and the `late` flag is the same
expression, evaluated per event. `_same_payload` in `app/ingest.py` is the one payload comparison both routes use.

Success body: `{"accepted": N, "duplicate": N, "results": [{"idempotency_key", "status", "event_id", "late"}, ...]}`, one result per
input event in input order. `status` is `accepted` or `duplicate`, `event_id` and `late` are those of the stored row.

| Status | When |
|--|--|
| 201 | at least one event was newly stored |
| 200 | every event was already stored (a resend of a whole batch): nothing stored |
| 403 | an event's `tenant_id` is not the tenant the edge admitted; the detail names the index (`events[i]`), nothing is stored, the check runs before any database work |
| 409 | an event reuses a key with a different payload, either against a stored event or against another event in the same batch; the detail names the index (or both indexes), nothing is stored |
| 413 | the body is over 7,363,012 bytes (`MAX_BATCH_BODY_BYTES`, below). Same mechanics as the single route's 413: from `Content-Length` or while the stream arrives, before parsing and before the edge runs |
| 422 | invalid body: an invalid event (the same rules as the single route; `loc` names the index, never the input), no events, more than 1000 events, or events spanning more than 48 UTC hours |
| 400, 401 / 402 / 503, 504, 503 | as for the single route (unparseable body; the edge refused; a statement past 15 s; the database went away or a lock wait ran out, `Retry-After: 1`). Nothing is stored in any of them |

**One bad event rejects the other 999.** The 4xx names the index of the bad event (a 422 lists every invalid one; a 403 or 409 the first), so a client fixes or drops that one and
resends the rest. Events from different tenants cannot be mixed in a batch: the first one that is not the edge's tenant gives 403.

**Repeats inside a batch.** A key sent twice in one batch is one event: the first copy is stored (`accepted`), a later identical
copy is reported `duplicate` with the same `event_id`, a later different copy is a 409 naming both indexes. This is decided in
Python before the database is opened, because the INSERT would otherwise keep an arbitrary copy.

**How it stays correct.**
- One `INSERT ... SELECT ... FROM unnest(...) ORDER BY tenant_id, idempotency_key ON CONFLICT (tenant_id, idempotency_key) DO NOTHING
  RETURNING`. The keys that did not insert are read back in one query and compared with `_same_payload`. The `ORDER BY` makes every
  batch insert in the same key order, so two overlapping batches queue on the unique index instead of deadlocking.
- **Hour locks:** before the INSERT, one shared hour lock per distinct UTC hour, each its own statement, in ascending hour order and held to commit,
  as the single route does for its one hour (see Rollups). The lock must come before the INSERT's snapshot, so it is never folded into the INSERT.
  Ascending order keeps two batches from waiting on each other's hours. An event is therefore in its closed rollup or flagged `late`,
  never neither; `test_ingest_batch.py` races batches against `close_hour` for this.
- **Why at most 48 hours:** each distinct hour is one lock in Postgres's shared lock table (6,400 slots by default), and each lock wait can cost the full 2 s,
  so H hours can wait up to H x 2 s in total (each wait is bounded on its own, and one that runs out answers 503; the 15 s statement timeout is per
  statement and each lock is its own statement, so the total is not capped by it). A real batch spans a few hours. A full lock table fails the lock request with "out of shared memory", a 503; it does not block other sessions.
  Hours are counted in UTC, whatever offset the timestamps are written in.
- **Why a JSON array plus `unnest`, not NDJSON plus `COPY`:** the body is validated by the same pydantic model as the single route, so the rules cannot drift
  apart, and one INSERT with `ON CONFLICT` gives the single route's dedupe and `late` rule per row; `COPY` cannot skip a conflicting row, and a staging table would be more machinery for the same result.

**The body cap.** `MAX_BATCH_BODY_BYTES` = 7,363,012 is the size of 1000 maximal events in the `{"events": [...]}` envelope: three
200-character non-BMP text fields (12 bytes per character as the `\ud83d\ude00` escape Python's `json.dumps` writes), both token counts 10^12 and the longest timestamp spelling,
in `json.dumps` spacing. A body one byte over is a 413; padding such as extra fractional digits or whitespace can push a valid batch over it, as on the single route.

**Memory of one maximal request (measured 2026-11-11).** One request carrying exactly 7,363,012 bytes (1000 maximal events), sent through the real app with
`TestClient`, parsed and validated (1000 `UsageEvent` objects) and stopped by a 403 tenant mismatch on the first event (the edge replaced by a fixed tenant, so no
database work), peaked at 17.0 MiB of Python allocations (`tracemalloc`, peak above the baseline, three runs, 17.0 MiB each; about 2.4 times the body). The peak
includes the test client's own copy of the body, so the server's share is somewhat lower.
That is one request's peak, and it is not a bound on the service. FastAPI reads the body and parses the JSON in the event loop (`await request.body()`,
`fastapi/routing.py`), before the entitlement check and before the request goes to a worker thread. So every request in flight holds its body, and every
request queued for a worker thread holds its parsed events: the 40 worker threads do not limit it. The memory is about 17 MiB times the number of such requests in flight, and
the app does not bound that number (uvicorn's connection limit and a proxy in front are the only bounds). For example, 40 such requests would hold about 680 MiB, but
that is an illustration, not a measurement and not a maximum. The same holds for the single route at its 64 KiB cap, with a much smaller figure per request.

**Measured (2026-11-11, Postgres 16 in a container on a WSL2 laptop; times move with the machine, no throughput claim).**
- Validating the body (pydantic plus the checks above, no database) took about 1.5 ms for 1000 ordinary events and about 3 ms for 1000
  maximal ones, so it runs on the request thread; it is not moved to a worker.
- **1000 maximal events do not fit in the statement timeout.** 1000 events whose three text fields are near-identical 200-emoji strings exceeded the 15 s
  statement timeout (504, nothing stored). The cost is the glibc `en_US.utf8` collation comparing such strings in the unique index, about 40 ms per row;
  the single route pays the same per event. A real batch of ordinary events is far from it, and the tests do not insert 1000 maximal rows for that reason (they
  insert 10, and check the cap by size alone).

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

**Tests:** `scripts/test tests/test_rollup.py tests/test_due_hours.py` (the second compares `DUE_HOURS` with the old query and guards its plan). The lock tests hold one side's lock by hand and
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
  finishes. Applying the schema again afterwards does nothing. Measured (2026-11-11, one machine, Postgres 16 in docker): the build took 788 ms
  on 1,000,000 rows (50 tenants, inserted in time order); on a much larger table the time grows with the rows (an estimate, not measured: tens
  of millions of rows reach 15 s). `db.connect()` gives the connection the request path's 15 s statement timeout, and `apply_schema` runs under
  it, so a build that takes longer is cancelled (`QueryCanceled`, checked: nothing is applied, the one transaction rolls back, and the index
  is not left half built). For the first apply on a large table use `db.connect(statement_timeout_ms=0)`, which is what the rollup uses.
- **The rollup hour-range index (`usage_rollups_hourly_tenant_hour`, `(tenant_id, hour_start, model)`).** The charges read ranges on
  `hour_start`, and the primary key leads with `model`, so its plan read the tenant's whole key range. Measured (2026-11-12, one
  machine, Postgres 16 in docker, 501,600 rows for one tenant: 114 models x 4,400 hours): a 1-day range returned 2,736 rows and took 20 ms
  through the primary key (about 4,100 index pages, the whole tenant range) and 1.1 ms through the new index (about 2,750 buffers in all);
  the planner chose a sequential scan of all 501,633 rows (24.5 ms) before the index existed. A 1-week range returned 19,152 rows. Building
  it on an existing database needs a manual `apply_schema` (README.md, Docker section); the build takes a write lock on
  `usage_rollups_hourly`, so closing hours and ingests that write rollups wait until it finishes. Use `db.connect(statement_timeout_ms=0)` for the first apply on a large table.

**Timeouts:** every connection gives up connecting after 5 s; request-path connections cancel a statement after 15 s,
so a database that stops accepting connections is a 503, and a statement past 15 s is a 504 `query timed out` on every route after the entitlement edge (the edge answers 503 for its own read; `POST /v1/usage` re-raises it to the handler in `app/main.py`). A lock wait over 2 s and a failed connection stay 503. `/healthz` is an `async` route that touches
nothing, so it keeps answering while every worker thread is busy; it shows that the process is up and its event loop
answers, not that the database does.
**Connection pool (`app/db.py`, `request_connection`).** The request path borrows connections from a bounded pool, one per
uvicorn worker process, opened on first use. `DB_POOL_MAX` (default 40, the size of the anyio thread pool that runs the
handlers) caps the connections one worker holds; `DB_POOL_TIMEOUT` (default the 5 s connect timeout) is how long a request
waits for a free one. A wait that runs out is a `PoolTimeout`, an `OperationalError`, so it is the same 503 as a refused
connection (edge: `entitlement check unavailable`; handler: `database unavailable`, `retry-after: 1`), and the log line
`no pooled connection came free: {pool stats}` names it. Each worker has its own pool: workers x `DB_POOL_MAX` must stay
under the Postgres `max_connections` (100 by default), or lower `DB_POOL_MAX`. A bad value for either variable raises
`ValueError` naming it. Every borrow is a clean session (autocommit off, 15 s statement timeout, UTC, no prepared-statement
cache: `prepare_threshold=None`), and a borrowed connection is checked with `check_connection` before use, so a backend that
was killed or restarted is replaced, not handed out. That check has no timeout of its own: it does **not** close the frozen
database gap below. The earlier fence (a dependency that held a connection while it waited for a worker thread used up the
Postgres slots) is kept by two short checkouts and never one held across the edge/handler boundary; with a bounded pool, a
held one would deadlock, so a thread that asks for a second checkout while holding one gets a `RuntimeError` at once.
**How long a checkout lives.** An ingest checkout lasts one transaction (milliseconds). A charges checkout lasts the whole streamed
pass (one repeatable-read transaction, `docs/billing.md`): about 2 s for a year of 114 models (1,002,096 rows, measured without the pool on
one machine), up to the 15 s deadline for a bigger range. So a few big charges requests hold slots for seconds while ingest needs
milliseconds. Estimate, not measured under the pool: with `DB_POOL_MAX=40` and 2 s passes, about 20 such passes a second fill the
pool (40 / 2 s), and a request that waits more than `DB_POOL_TIMEOUT` (5 s) for a slot is a 503; size `DB_POOL_MAX` for the number of
long charges requests you expect at once plus the ingest load, within the workers x `DB_POOL_MAX` limit above.
The rollup, reconcile, the delinquent job and the mirror CLI keep `db.connect()` (own connection, no statement timeout where
they had none). Tests: `tests/test_pool.py`.
A flood of 300 + 300 requests (`scripts/flood.py`, 150 clients, a hand-run check and not a load test; the peak is sampled, so it is
approximate): before the pool the wall time was 4.51 s and 4.32 s with a sampled peak of 35 and 34 backends; with the pool at 40
it was 3.03 s and 2.79 s with a peak of 36 and 40; with `DB_POOL_MAX=10` the peak was 10. Every run gave 300 x 201 and 300 x 200.
Two runs each on one machine do not show a gain in throughput; what they show is that the cap holds. `scripts/outage-check.sh`
passed against the pooled stack (503 while the db was stopped, 201 after, no API restart).
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
