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
`closed_hours` row (the earliest 100, oldest first, the current hour included; `[]` when nothing is waiting). The total is
complete only when the list is empty (a list of exactly 100 means at least 100 hours are waiting; there is no flag for the
cut, because any non-empty list already says the total is partial). This is also how a stuck rollup shows on the bill side: an older hour that stays in the
list long after its 65 minutes is one the rollup cannot close (see the rollup log), and every later hour is billed without it.
Model names are compared byte for byte: `GPT`, `gpt` and `GPT ` are three models, each needing its own price.
Status codes: 403 if the caller's tenant is not the path tenant, 422 for naive datetimes, a number instead of ISO text,
`start >= end`, or a range wider than 366 days (ask for the period in pieces: every row of the range is loaded at once),
409 if a row has no price in force: the whole range is refused (no partial bill), and the detail names every unpriced model
in the range, sorted, with the first hour of each (at most 20, then "and N more"), so all the missing prices can be added in
one pass. The answer has one line per tenant, model and hour, and no cap on the number of lines is built: the red team
asked for 366 days of 114 models (about 1M rows) and got a 114 MB body in 3.6 s (RT-operations-11, in [docs/reviews/](reviews/README.md); not repeated here).
Ask for narrower ranges. The route sits behind `require_entitlement`, so
the edge can also answer 401, 402 or 503 as in the edge table below.

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
It opens its own database connection (one extra per request), so a database that is down answers 503 instead of 500.

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
- **Two database connections per request.** The edge opens its own and the handler opens another, with no pool, so a busy
  database runs out of connections sooner. That is a `psycopg.OperationalError`, which both the edge and the central handler
  turn into 503; not tested under load. The handler takes its connection itself, only once it holds a worker thread and after
  the checks that need no database, so a refused charges request (403 or 422) opens none (`tests/test_charges_connection.py`).
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
