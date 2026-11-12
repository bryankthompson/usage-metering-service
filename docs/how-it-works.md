# How it works

The nine parts of the service, in the order a request meets them. The [README](../README.md) has the overview and a diagram.

**1. A usage event comes in.** `POST /v1/usage` (`app/ingest.py`) takes `tenant_id`, `idempotency_key`,
`model` (1 to 200 characters each), `tokens_in`, `tokens_out` (integers, 0 to 10^12) and `occurred_at`
(ISO 8601 text with a time zone).
One `INSERT ... ON CONFLICT (tenant_id, idempotency_key) DO NOTHING` does the dedupe, so two identical
requests at the same instant store one event. New event: **201** `accepted`. Resend with the same key and
payload: **200** `duplicate`, nothing stored. Both bodies carry `event_id` and `late`. Same key with a
different payload: **409**. Body `tenant_id` is not the
tenant the edge admitted: **403**. Invalid body: **422**. A body over 64 KiB: **413**: an oversized
`Content-Length` is rejected immediately; otherwise streamed chunks are counted and the request is rejected as soon as
they pass the cap, without buffering the rest (also with no credentials). A body that is not JSON: **422**
(`json_invalid`). JSON nested very deep: **400** (measured inside the 64 KiB cap, 422 up to about 9,882 levels and 400
from about 10,077, so the boundary lies between those two numbers).

`POST /v1/usage/batch` takes `{"events": [...]}`, 1 to 1000 events occupying at most 48 distinct UTC-hour buckets (else **422**; the elapsed date range may be longer), each
validated and deduplicated exactly as above, in one transaction: if any event is refused (403, 409, 422) or the database fails,
nothing is stored and the whole batch can be resent. It answers **201** with `accepted`, `duplicate` and one `results` entry per event
(`idempotency_key`, `status`, `event_id`, `late`) in input order, or **200** when every event was already stored. A key repeated inside the
batch is one event; a repeat with a different payload is a 409 naming both indexes. The body cap is 7,363,012 bytes (413). Details,
limits and measurements: [docs/ingest.md](ingest.md).

**2. Into an append-only log.** Events are stored in `usage_events` (`db/001_usage.sql`); a trigger rejects
every row UPDATE and DELETE; the trigger does not stop TRUNCATE, or the table owner or a superuser (who can disable the
trigger). In compose the application connects as that superuser, so the trigger guards against mistakes, not against the
service's own credentials. The application never rewrites an accepted event.

**3. Hourly rollups.** `app/rollup.py` sums one UTC hour of events per tenant and model into
`usage_rollups_hourly` and records the hour in `closed_hours`, in one transaction. `python -m app.rollup`
closes every hour that has events and ended at least 5 minutes ago (`--every SECONDS` repeats; compose runs
it hourly as the `rollup` service). Late and duplicate events never change a closed hour: an event whose
UTC hour is already closed is stored with `late = true` and answered 201 with `"late": true` (a resend says
so too), but it is left out of the rollup, so it is never charged. Ingest takes a shared advisory lock on the event's hour and closing
takes the exclusive one, so every event of a closed hour is either in its closed rollup or flagged late,
never both and never neither. Each hour is closed on its own: if one fails (for example a sum past BIGINT) it is rolled
back, logged as `rollup failed: hour=...` and skipped, the hours after it still close, a one-shot run exits 1, and the
`--every` loop retries it each cycle. Details and the known limits: [docs/ingest.md](ingest.md).

**4. Versioned prices in micro-USD.** `app/pricing.py`. Each model has numbered price versions with an
`effective_from` and two integer prices, micro-USD per 1,000 input and per 1,000 output tokens (1 USD =
1,000,000). A new price is a new version; a database trigger rejects changing or deleting a stored one.
`price_at` picks the version in force at a time, and raises `NoPrice` when there is none: a missing price
is never rated as zero. A rate is `(tokens_in * input_price + tokens_out * output_price) / 1000` in integer
arithmetic, rounded half up once. `GET /v1/tenants/{tenant_id}/charges` rates each hourly rollup row at the price in
force at the start of its hour, so a price change applies from the first hour that starts at or after its effective time
(a version effective at 10:30 applies from the 11:00 hour). Nothing stores a bill. Charges cover closed hours only:
`unclosed_hours` in the answer lists the hours in the range that hold usage but are not closed yet (the earliest 100, and
`unclosed_hours_truncated` says when there were more), so a total with a non-empty list is partial. A range with more than
10,000 lines is read in pages (`limit`, `cursor`): every page carries the whole range's `total_micros`, and the lines of all
pages must add up to it (docs/billing.md, Paging). Use `start` and `end` on UTC hour boundaries: an
hour is in or out of a range by its start, so a range that cuts an hour includes or drops all of it. **403** for another tenant, **422** for a naive timestamp, a `start` or `end` that is
not ISO 8601 text, `start >= end`, a range wider than 366 days, or a `limit`, `cursor` or unpaged range that breaks the paging rules, **409** when a row has no price in force (the message
names the models without one, with the first hour of each; past 20 it says how many more) or when the range changed under a cursor. Details: [docs/billing.md](billing.md).

**5. Stripe test-mode webhooks set the entitlement.** `POST /v1/stripe/webhook` (`app/webhooks.py`) checks
the `Stripe-Signature` header on the raw body with `STRIPE_WEBHOOK_SECRET` before it touches the database:
no secret configured **503**; missing, bad or older-than-5-minutes signature **400**. Then:
- `customer.subscription.created`, `.updated` and `.deleted` write the tenant's entitlement row, keyed by
  `subscription.metadata.tenant_id`: `active` and `trialing` become `active` (clearing any failure and grace
  period), `past_due` becomes `past_due` and keeps an existing grace period, any other Stripe status becomes
  `canceled`, and `.deleted` is always `canceled`. One subscription per tenant is assumed: deleting any
  subscription that carries the tenant's id cancels the tenant, so change a plan by updating the same one. An event
  that shares the second of a cancel never reactivates the tenant (Stripe's `created` has one-second resolution and
  delivery order is not guaranteed); a strictly newer `active` event does.
- `invoice.payment_failed` finds the tenant by the customer id an earlier subscription event stored (an
  unknown customer is `ignored`), sets `past_due` and a grace period of `GRACE_PERIOD_HOURS` (default 72) counted
  from the first failure that arrives: Stripe's retries of the same failed invoice do not extend it, and an `active`
  subscription event clears it. It does not reopen a `canceled` tenant. If `GRACE_PERIOD_HOURS` is not an
  integer from 0 to 8760, a failure that has to open a grace window is **503** and not recorded, so Stripe
  retries it; other events are unaffected.
- Each event id is stored in the same transaction as its change: a redelivery is **200** `duplicate`. An event
  older than the last one applied does not overwrite newer state (`stale`; two narrow exceptions, an older payment
  failure and a delete of the stored subscription, are in [docs/billing.md](billing.md)). Unknown tenants and unhandled event types
  are `ignored`. Past the signature check every answer is 200 `{"status": ...}`, except a malformed event
  (**400**), a body over 1 MiB (**413**) and a database failure (**503**; Stripe retries it).

**6. A fail-closed edge.** `require_entitlement` (`app/edge.py`) guards `/v1/usage` and the charges route.
The tenant is the `X-Tenant-Id` header. Missing header: **401**. No entitlement row, `canceled` or any other
status: **402**. `active`: allowed. `past_due`: allowed while now is no later than `grace_until`, **402** after it or when no grace period is recorded.
Any error reading the entitlement: **503**, never allowed. A database failure later in a request is **503** too (one handler for `psycopg.OperationalError` in `app/main.py`), with `Retry-After: 1`. So are
a wait of more than 2 seconds for a lock (resend: nothing of the failed attempt is stored) and a connection that takes more than 5 s to open or to borrow from the pool
(the edge's own 503, "entitlement check unavailable", carries no `Retry-After`). A statement that runs more than 15 s on the request path is
**504** `{"detail": "query timed out"}` with no `Retry-After` (the same query would time out again) on every route after the entitlement edge,
`POST /v1/usage` included; the entitlement edge catches it itself and still answers 503. The
rollup, reconcile and delinquent runs have no statement timeout.

**7. Two read-only reports.** `python -m app.reconcile` recomputes every closed hour from the raw log with its own SQL and
exits 0 when the stored rollups agree, 1 on drift (including a rollup row whose hour is not closed), 2 on bad input;
`--actuals FILE.jsonl` also compares them with totals from another source. It always prints what is not billed (late
events, events over 2 hours past their hour and still not closed, events dated over 1 hour ahead); `--fail-on-unbilled`
makes any of them exit 1, `--json` prints one object ([docs/reconcile.md](reconcile.md)). `python -m app.delinquent` lists `past_due` and `canceled` tenants
with their grace state (`--json`, `--fail-on-expired` for a cron job, `--at ISO`) using the edge's own grace rule
([docs/billing.md](billing.md)).

**8. An optional ClickHouse copy.** With `CLICKHOUSE_URL` set, each closed hour's rollup rows are also written to ClickHouse
(`docker compose --profile analytics`); a failure there is logged and never touches the bill, `python -m app.mirror --backfill`
recovers it, and `--export-actuals` feeds `app.reconcile --actuals` ([docs/clickhouse.md](clickhouse.md)). It is a derived copy, not the bill.

**9. A platform adapter (`seekrflow`), not run against the real service.** `app/seekrflow.py` turns the `usage` object of an inference
response (chat, embeddings or the final stream chunk) into one usage event, with the response id as the idempotency key; a
missing usage raises and is never counted as zero. It was built from the public API documentation and tested with
hand-written fixtures, the real app and a local stand-in server. No call to the real service has been made: there is no key.
[docs/seekrflow.md](seekrflow.md) lists what was and was not verified.
