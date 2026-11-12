# Billing: price table and rating

Money is an integer count of micro-USD (1 USD = 1,000,000). No float and no true division in the money path.

## Prices
- `price_versions` holds, per model, numbered versions (1, 2, ...) with an `effective_from` and two integer
  prices: micro-USD per 1,000 input tokens and per 1,000 output tokens.
- `add_price_version` always adds the next version. A stored version is never changed or removed: a database
  trigger rejects UPDATE and DELETE; the trigger does not stop TRUNCATE, or the table owner or a superuser (who can disable the trigger).
- `price_at(model, at)` returns the version with the greatest `effective_from <= at` (a tie goes to the later
  version). No version in force raises `NoPrice`; a missing price is never rated as zero.
- Charges rate a whole hour at the price in force at the START of that hour, so a version applies from the first
  hour that starts at or after its `effective_from`. A version effective at 10:30 first applies to the 11:00 hour;
  usage from 10:30 to 11:00 is rated at the earlier price. Use an hour boundary for `effective_from` if you mean one.
- Prices are only added, not edited (the trigger limits are above), but a bill is not stored (see Charges): a version added
  later with an earlier `effective_from` changes what charges returns for the hours after it.

## Rating
`rate` = `(tokens_in * input_price + tokens_out * output_price)` divided by 1,000 in integer arithmetic, rounded
half up ONCE on that sum. Example: 1 token at 1,500 micros per 1k is 1.5, which becomes 2.

## Charges
`GET /v1/tenants/{tenant_id}/charges?start=...&end=...` (ISO 8601 text with a time zone, such as
`2026-11-01T00:00:00Z`; a bare number is not read as Unix seconds) rates the tenant's
`usage_rollups_hourly` rows with `start <= hour_start < end`. Each row is rated at the price in force at its own
`hour_start`, so a price change applies from the first hour that starts at or after its `effective_from` (a version
effective at 10:30 applies from the 11:00 hour). Each row is rounded on its own; `total_micros` is the sum of lines.
Charges are computed when asked and no bill is stored: that is a choice (no snapshot), so the same range asked again
after a back-dated `add_price_version` can return a different total. Keep your own invoices.
Charges cover closed hours only. Usage in an hour that has not been rolled up yet (the current hour, and an ended hour for up
to about 65 minutes: the 5-minute grace plus one run of the hourly rollup loop) is not in `total_micros`. The response says
which hours those are: `unclosed_hours` lists the hours that start inside the range, hold usage that is not late and have no
`closed_hours` row (the earliest 100, oldest first, the current hour included; `[]` when nothing is waiting).
`unclosed_hours_truncated` is `true` when there were more than 100 (the list is cut at 100; the flag says so). The total is
complete only when the list is empty. The list is the same on every page. This is also how a stuck rollup shows on the bill side: an older hour that stays in the
list long after its 65 minutes is one the rollup cannot close (see the rollup log), and every later hour is billed without it.
Model names are compared byte for byte: `GPT`, `gpt` and `GPT ` are three models, each needing its own price.
Status codes: 403 if the caller's tenant is not the path tenant, 422 for naive datetimes, a number instead of ISO text,
`start >= end`, a range wider than 366 days (ask for the period in pieces), or a paging error (below),
409 if a row has no price in force: the whole range is refused (no partial bill), and the detail names every unpriced model
in the range, sorted, with the first hour of each (at most 20, then "and N more"), so all the missing prices can be added in
one pass; 409 `range changed, restart` under a cursor (below); 504 `query timed out` when the pass runs past the request's
15 s statement timeout. The route sits behind `require_entitlement`, so the edge can also answer 401, 402 or 503 as in the edge table below.

### Paging
The answer is `{tenant_id, start, end, lines, total_micros, line_count, next_cursor, unclosed_hours, unclosed_hours_truncated}`.
`total_micros` and `line_count` are the WHOLE range's, on every page.
- **No `limit`**: all the lines of the range, `next_cursor: null`. If the range holds more than `LINE_CAP` (10,000) lines the answer
  is 422 `page with limit`, never a 200 with fewer lines.
- **`limit`** (1 to `MAX_PAGE`, which is `LINE_CAP`): at most that many lines, in `(hour_start, model)` order, and `next_cursor`
  (opaque text) when more follow, `null` on the last page. Send it back as `cursor` with the same `limit`, tenant, `start` and
  `end` for the next page. A `cursor` without a `limit` is 422, and so is a malformed one or one made for another tenant or range
  (`foreign cursor`; the same instant spelled in another UTC offset is the same range). Order is the database's (`model` is
  compared only in SQL, with the column collation), so `Zebra` follows `apple`.
- **All or nothing, on every page.** Each request rates every row of the range, so a missing price anywhere is a 409 on page 1,
  before any line is returned.
- **The client rule.** Every page reports the whole range's `total_micros` as of its own request (it can differ between requests if hours close meanwhile): never add the totals of the pages (that bills the
  page count times). Add the `micros` of ALL the lines you hold: it must equal the last page's `total_micros`, and the number of lines
  must equal its `line_count`; if not, start over from page 1. The service enforces part of this itself: the cursor holds the count
  and cost of the lines up to it, and a page asked with a cursor whose prefix no longer matches (an hour closed out of order behind
  the cursor, a price version backdated) is 409 `range changed, restart`. An hour that closes AHEAD of the cursor between two requests
  is not an error: it comes in a later page and the last page's total includes it, which is what the rule checks. The check
  compares the count and the sum of `micros` of the lines before the cursor, and nothing more: two changes in that prefix that
  offset each other (one line up by x, another down by x, for example two backdated price versions) are not detected. The lines
  the client holds may then be stale line by line, but the total is still the right bill and the client rule still holds.
  `scripts/demo.sh` and `scripts/scenario.py` apply the rule.
- **N pages cost N full passes.** The server keeps no state between requests; each page reads and rates the whole range again
  (constant memory, a server-side cursor, but the CPU is O(range) per page). A client that walks a year of 114 models
  (about 1M lines) in pages of 10,000 makes about 100 passes over 1M rows. Take large pages, and do not poll.
- **One snapshot.** The unclosed-hours read, the rows and the prices of one request are read in one repeatable-read transaction.
  "An hour that closes meanwhile is listed and billed, never neither" holds within a page; across pages the client rule is the check.
- **Deadline.** The statement timeout is per statement, so a loop of fetches would escape it; the pass checks its own clock and
  raises the same `QueryCanceled`, answered 504 `query timed out`, once it has run past `REQUEST_STATEMENT_TIMEOUT_MS`.
- **Why 10,000.** The 114 MB body of a year of 114 models (about 1M rows, RT-operations-11 in [docs/reviews/](reviews/README.md)) is
  now historical: it can no longer be built. A body of `LINE_CAP` lines is about 1.2 MiB and peaked at about 6 MiB of Python
  allocations in `tests/test_charges_paging.py` (tracemalloc, one request), so 40 concurrent ones are about 230 MiB; the first page
  of 1,002,096 rows peaked at under 1 MiB. Numbers of one run on one machine, not a promise.
- **Time of a pass.** Measured on 2026-11-11 on one development machine (Postgres 16 in docker on the same host), through the real
  route with `limit` 100, best of 3: a full-year range of 1,002,096 rows (114 models x 366 days) took 2.07 s on the first page and
  2.06 s on a page near the end of the range (runs of 2.07 to 2.24 s and 2.06 to 2.19 s), about 485,000 rows per second. A page
  late in the range costs the same as the first, since every page rates every row. The whole pass has one deadline,
  `REQUEST_STATEMENT_TIMEOUT_MS` (15 s). A range whose pass exceeds it is a 504 `query timed out` on every page, because every
  page repeats the same pass: the client must then ask for a shorter range. Rows divided by 485,000 gives your range's pass in
  seconds on a comparable machine (a slower or busier host, or concurrent requests, will be slower), so 15 s corresponds to about
  7 million rows. The year of 114 models measured here is a seventh of that; a tenant with about 800 models over a year (8,784 rows each) would reach it.
  One run's figures, not a promise.
- **Pool slot.** The pass runs inside one checkout from the request pool (`db.request_connection`, `docs/ingest.md`), so a request
  holds a slot for its whole pass: about 2 s for the year above, up to the 15 s deadline. Forty such requests at once take every slot of
  a worker with the default `DB_POOL_MAX` of 40, and ingest requests then wait up to `DB_POOL_TIMEOUT` (5 s) and answer 503. An
  estimate from the figures above, not a measurement under the pool.
- **Index.** `(tenant_id, hour_start, model)` on `usage_rollups_hourly` was measured and not added. On 1,083,456 rows (114 models x
  366 days for one tenant plus a month of another), `EXPLAIN (ANALYZE, BUFFERS)` of the whole-year read: without the index a
  parallel sequential scan plus an external-merge sort (about 48 MB of temporary files in three workers), 557 ms; with it an index scan
  with no sort, 268 ms. One week (19,152 rows): 30 ms against 18 ms. A full pass in Python over 1,002,096 rows takes about 2.1 s (see "Time of a pass"), so the sort is
  not what a page waits for, and the index would cost 60 MB (the table is 87 MB) and a write on every hourly close. Revisit when
  the table holds many tenants' history: the sequential scan reads all of it for any range, and the index would not.

## Tests
`DATABASE_URL=... uv run pytest -q tests/test_pricing.py tests/test_pricing_charges.py` (needs Postgres, see `compose.yaml`).

## Not done
No admin API: prices are added through `add_price_version`. Nothing is sent to Stripe and no invoice is made.

# Billing: Stripe webhooks and the edge check (test mode)

`POST /v1/stripe/webhook` checks `Stripe-Signature` on the raw body (`stripe.Webhook.construct_event`, secret
`STRIPE_WEBHOOK_SECRET`) before it touches the database. No secret set (or a blank one): 503. A body over 1 MiB: 413
(it is read before the signature can be checked, so it is capped). Missing or bad signature, a timestamp older than
5 minutes, or an unparsable body: 400. A database that fails or cannot be reached mid-request (`psycopg.OperationalError`) is
503, not 500, with `Retry-After: 1` (the handler in `app/main.py`); a statement past the 15 s statement timeout is 504 `query timed out` with no `Retry-After` (its own handler there). Two 503s carry no `Retry-After`: the edge's
`entitlement check unavailable` (`app/edge.py`, the API cannot read the entitlement), and the webhook's own refusals for a
missing secret or an unusable `GRACE_PERIOD_HOURS` (`app/webhooks.py`). A wait of more than 2 seconds (`db.LOCK_WAIT`) for a
held lock, such as the tenant's advisory lock or its entitlement row, is a 503 with `Retry-After: 1` too: the event is not recorded
and Stripe's retry applies it. The rollup (closing) side never times out. `/healthz` does not touch the database and keeps
answering 200. The tenant is `subscription.metadata.tenant_id` (the row is created or updated) or, for invoices, the
`entitlements` row with that `stripe_customer_id`. No tenant found: 200
`{"status": "ignored"}`, logged, not recorded. Other event types are ignored the same way, and so is a subscription
whose `tenant_id` is not usable text: over 200 characters (the limit `/v1/usage` has), a control character or a lone
surrogate in it, or a space or tab at either end (the edge trims the header, so such a tenant could never match),
unless an entitlement row with exactly that id already exists: a row stored before the rule (or by hand) is still
reached, so a cancel for it is not lost.

| Event | Effect |
|---|---|
| `customer.subscription.created/updated/deleted` | `plan` = Stripe price id. `active`/`trialing` -> `active`, clears the failure and grace. `past_due` -> `past_due`, keeps an existing grace window. Any other status -> `canceled`. `deleted` is always `canceled`, whatever status its payload carries. A cancel clears the failure and the grace window, so a later `past_due` event starts with none. A `deleted` for the stored subscription id is never `stale` (below). |
| `invoice.payment_failed` | `past_due`, `payment_failed_at` = event `created`, `grace_until` = that + `GRACE_PERIOD_HOURS` (default 72). A `canceled` tenant stays canceled. |

Grace runs from the first failure of a delinquency THAT ARRIVES. Stripe retries a failed invoice for days, so a later
`invoice.payment_failed` for a tenant that is already `past_due` with a window keeps `payment_failed_at` and
`grace_until` and only records `last_event_at`. That first-arrived failure is Stripe's first failure only when delivery
order equals creation order; if a retry is delivered first, the window counts from the retry and the older failure that
arrives afterwards does not move it (a difference of seconds to minutes with Stripe's retry spacing). An `active`
subscription event clears the window; the next failure starts a new one. `invoice.paid` is not handled: only `customer.subscription.*` events restore `active`.

Each event id is recorded in `stripe_events` in the same transaction as the change; a redelivery is 200 `duplicate`.
An event older than `last_event_at` does not overwrite newer state (200 `stale`); the clock is shared by invoice and
subscription events. One exception is a `customer.subscription.deleted` whose subscription id equals the one stored on the
tenant: it is terminal, so it is applied whatever `last_event_at` says (a payment failure created after the cancel and applied
first does not hide it), and `last_event_at` keeps the larger value. A delete of another id, or for a tenant with no stored id,
is judged by the clock (see "Assumptions and limits"). Events with the same `created` second
apply in arrival order (Stripe's `created` has one-second resolution and gives no order inside a second), with one rule:
**a tie never undoes a cancel.** A `customer.subscription.created/updated` whose `created` equals the `last_event_at` of
a `canceled` tenant is `stale` and logged (`webhook tie after cancel`), so a late `updated(active)` of the cancel's own
second cannot reactivate the tenant; only a strictly newer subscription event does. A `deleted` of that second still
applies, and so does a tie in any other state. An `invoice.payment_failed` of the cancel's second does not reopen the
tenant either (a failure never does). Cost: a genuinely new subscription created in the same second as the old one's cancel
(a plan swap done as delete plus create) stays denied until a strictly newer subscription event arrives: it fails closed.
The other exception to `stale`: an older failure for a `past_due` tenant that has no window opens one, counted from that
failure's own time (200 `processed`, `last_event_at` unchanged). It can be shorter than a fresh window if the old
failure belongs to an earlier, cured delinquency, and with failures delivered out of order by more than the retry
spacing it can be later than Stripe's first failure. A later older failure does not move an existing window.

`GRACE_PERIOD_HOURS` must be an integer from 0 to 8760, otherwise an `invoice.payment_failed` that has to open a grace
window answers 503 and is not recorded (Stripe retries); other events are unaffected. Assumption: one Stripe customer is one tenant; two tenants sharing a customer id would have
invoice events applied to an arbitrary one, which is not handled.
Assumption: one subscription per tenant. A `customer.subscription.deleted` for any subscription carrying the tenant's
`metadata.tenant_id` cancels the tenant, so change a plan by updating the same subscription; with two live
subscriptions, ending either denies the tenant (fail closed) until a newer subscription event for the other arrives.

The edge (`require_entitlement`) reads tenant from the `X-Tenant-Id` header, a stand-in for real authentication.
It takes its own short checkout from the connection pool (one extra per request, given back before the handler takes its own), so a database that is down, or a pool with no free connection, answers 503 instead of 500.

| Case | Answer |
|---|---|
| header missing | 401 |
| no row, `canceled`, unknown status | 402 |
| `active` | allowed |
| `past_due` | allowed while `now <= grace_until`; 402 after, or with no `grace_until` |
| database error | 503, never allowed |

`entitlements.status` holds only `active`, `past_due` or `canceled`: the table has a CHECK constraint
(`entitlements_status_check`) and the database refuses any other value. `apply_schema` adds the constraint to a database
created before it existed, once (a second run does nothing). A legacy row outside the three values makes `apply_schema`
fail at that step; find such rows first with:

    SELECT tenant_id, status FROM entitlements WHERE status NOT IN ('active','past_due','canceled');

The edge still answers 402 for any other status it might read (the test stubs the row, since the table cannot hold one).

## Logging
Every webhook outcome is one INFO line in the container log (`docker compose logs api`):
`webhook processed|duplicate|stale|ignored: event='evt_...' type='...' tenant='...'`, with the ids quoted (`%r`) so a newline in
one cannot forge a second line. The edge logs its refusals (WARNING) and the charges route logs each bill (INFO). `LOG_LEVEL`
(`DEBUG`, `INFO`, `WARNING`, `ERROR` or `CRITICAL`, any case; default `INFO`; `compose.yaml` passes it to the `api` service)
sets how much of the `app.*` loggers is shown. A value that is not one of those falls back to `INFO` and logs a warning.

## Assumptions and limits
These are known and not fixed; see also the two assumptions above (one customer per tenant, one subscription per tenant).
- **A `start` or `end` that is not on a UTC hour boundary cuts no hour.** Rows are whole hours and the range test is on the
  hour's start, so `start=10:30` leaves out the whole 10:00 hour (an event at 10:45 is in the window and not in the bill),
  and `end=10:30` includes the whole 10:00 hour (usage after 10:30 is billed). `unclosed_hours` uses the same test and
  does not flag it. Ask for ranges on hour boundaries; two adjacent ranges that split at one never count an hour twice.
  Found by review; a `422` for a range off the boundary was considered and not built (a read-only request with a
  re-askable answer, and a caller in a +05:30 zone would be refused its own local day).
- **A tenant is not bound to a Stripe customer.** A subscription event for tenant X overwrites X's stored customer id
  with the event's. Repro: post a signed `customer.subscription.updated` for `victim` with customer `cus_victim`, then
  another for `victim` with customer `cus_other`; a later `invoice.payment_failed` for `cus_victim` is `ignored`, so the
  tenant keeps access. The signed event comes from Stripe and the metadata is set by the operator, so this needs a
  mistake or a bad actor on the operator's side.
- **`X-Tenant-Id` is spoofable and ambiguous.** It stands in for authentication. If it is sent twice, the first value wins
  (`t2` then `t1` is `t2`); `t1, t2` in one header is the tenant `t1, t2`, which has no entitlement (402).
- **A far-future `created` pins a tenant.** `last_event_at` takes the event's `created`, and a later event with an earlier
  `created` is `stale`. A signed event dated far in the future (a Stripe clock fault) would make real events stale for that tenant
  until the row is repaired. Not tested.
- **A cancel the stored subscription id does not recognise is judged by the shared clock** (RT-concurrency-4). A `deleted`
  is applied whatever `last_event_at` says only when its subscription id equals the stored `stripe_subscription_id`. A delete
  with another id, or for a tenant whose row holds none, that is created before an applied payment failure is `stale`: recorded
  as handled and not retried, so the tenant stays `past_due` until `grace_until` (72 hours after the failure by default).
  Assumption, not checked against Stripe: a canceled subscription id is not reopened. Pinned by `tests/test_cancel_order.py`.
- **Concurrency is tested with threads in one process.** Multi-process behaviour (several uvicorn workers) is not tested; the
  per-tenant advisory lock and the row lock are what it relies on.
- **Two pooled checkouts per request.** The edge borrows a connection from the pool and gives it back, and the handler borrows
  another, so a request uses two short checkouts and never two at once. The pool is bounded (`DB_POOL_MAX`, default 40, per
  worker process), and a wait past `DB_POOL_TIMEOUT` is a `PoolTimeout`, an `OperationalError`, so both the edge and the central
  handler turn it into 503; the cap holds in a hand-run flood (`docs/ingest.md`), which is not a load test. The handler takes its
  connection itself, only once it holds a worker thread and after the checks that need no database, so a refused charges request
  (403 or 422) takes none (`tests/test_charges_connection.py`, `tests/test_pool.py`).
- **The session time zone is pinned to UTC on every connection** (`db.connect` sets it in the statement that sets the statement
  timeout), so `PGTZ`, a URL option or the server's setting cannot change the hours the app prints or reads back. The whole suite
  passes under `PGTZ=America/New_York` (601 of 601 at 02e94c8). A connection opened by hand (`psql`) keeps its own zone.
- **`LOG_LEVEL` does not override a handler that is already installed.** `configure_logging` sets the level of the `app`
  logger, but a root handler with its own stricter level, installed before the app starts, still filters the lines. Under
  `docker compose` no such handler exists. Found in review, not fixed.

Tests: `scripts/test tests/test_webhooks.py tests/test_edge.py` (signed with a fake `whsec_test_...` secret).
The adversarial set is `tests/test_attack_webhooks.py`, `tests/test_attack_billing.py` and `tests/test_attack_db_outage.py`.
Not done: calling Stripe, usage push, invoices, real authentication.

## Delinquent report
`python -m app.delinquent` reads `entitlements` and lists every `past_due` or `canceled` tenant (read-only). `state` is
`in_grace` (whole hours left), `grace_expired` (whole hours ago), `past_due_no_grace` (denied, no grace recorded) or
`canceled`. Denied tenants come first, then `in_grace` by least time left. The rule is the edge's own (`edge.in_grace`:
`now <= grace_until` is still in grace) and the clock is `edge.now`.
- `--json`: one object per line; default is a plain table.
- `--fail-on-expired`: exit 1 when any `past_due` tenant is denied (`grace_expired` or `past_due_no_grace`), else 0.
- `--at ISO`: evaluate at that instant (needs a zone) instead of now. A bad argument exits 2.
Test: `scripts/test tests/test_delinquent.py`.
