# usage-metering-service

A small prototype of a usage metering and billing service. Tenants report usage events (a model and
input and output token counts); the service counts each event once, rolls the events up per hour, prices
them in integer micro-USD, and serves only tenants whose Stripe test-mode subscription is in good standing.
Python 3.12, FastAPI, plain SQL through psycopg, Postgres 16. Test mode only: nothing calls Stripe.

## How it works

**1. A usage event comes in.** `POST /v1/usage` (`app/ingest.py`) takes `tenant_id`, `idempotency_key`,
`model`, `tokens_in`, `tokens_out` (integers, 0 to 10^12) and `occurred_at` (a timestamp with a time zone).
One `INSERT ... ON CONFLICT (tenant_id, idempotency_key) DO NOTHING` does the dedupe, so two identical
requests at the same instant store one event. New event: **201**. Resend with the same key and payload:
**200** `duplicate`, nothing stored. Same key with a different payload: **409**. Body `tenant_id` is not the
tenant the edge admitted: **403**. Invalid body: **422**.

**2. Into an append-only log.** Events are stored in `usage_events` (`db/001_usage.sql`); a trigger rejects
every UPDATE and DELETE. Nothing that was accepted is ever rewritten.

**3. Hourly rollups.** `app/rollup.py` sums one UTC hour of events per tenant and model into
`usage_rollups_hourly` and records the hour in `closed_hours`, in one transaction. `python -m app.rollup`
closes every hour that has events and ended at least 5 minutes ago (`--every SECONDS` repeats; compose runs
it hourly as the `rollup` service). Late and duplicate events never change a closed hour: an event whose
UTC hour is already closed is stored with `late = true` and answered 201 like any other, but it is left out
of the rollup, so it is never charged. Ingest takes a shared advisory lock on the event's hour and closing
takes the exclusive one, so every event is either in the closed rollup or flagged late, never both and
never neither. Details and the known limit: [docs/ingest.md](docs/ingest.md).

**4. Versioned prices in micro-USD.** `app/pricing.py`. Each model has numbered price versions with an
`effective_from` and two integer prices, micro-USD per 1,000 input and per 1,000 output tokens (1 USD =
1,000,000). A new price is a new version; a database trigger rejects changing or deleting a stored one.
`price_at` picks the version in force at a time, and raises `NoPrice` when there is none: a missing price
is never rated as zero. A rate is `(tokens_in * input_price + tokens_out * output_price) / 1000` in integer
arithmetic, rounded half up once. `GET /v1/tenants/{tenant_id}/charges` rates each hourly rollup row at its
own hour, so a price change mid-period applies from its effective time. **403** for another tenant, **422**
for a naive timestamp or `start >= end`, **409** when a row has no price in force. Details:
[docs/billing.md](docs/billing.md).

**5. Stripe test-mode webhooks set the entitlement.** `POST /v1/stripe/webhook` (`app/webhooks.py`) checks
the `Stripe-Signature` header on the raw body with `STRIPE_WEBHOOK_SECRET` before it touches the database:
no secret configured **503**; missing, bad or older-than-5-minutes signature **400**. Then:
- `customer.subscription.created`, `.updated` and `.deleted` write the tenant's entitlement row, keyed by
  `subscription.metadata.tenant_id`: `active` and `trialing` become `active` (clearing any failure and grace
  period), `past_due` becomes `past_due` and keeps an existing grace period, any other Stripe status becomes
  `canceled`.
- `invoice.payment_failed` sets `past_due` and a grace period of `GRACE_PERIOD_HOURS` (default 72) from the
  event time. It does not reopen a `canceled` tenant.
- Each event id is stored in the same transaction as its change: a redelivery is **200** `duplicate`. An event
  older than the last one applied does not overwrite newer state (`stale`). Unknown tenants and unhandled event types
  are `ignored`. All answers are 200 `{"status": ...}`.

**6. A fail-closed edge.** `require_entitlement` (`app/edge.py`) guards `/v1/usage` and the charges route.
The tenant is the `X-Tenant-Id` header. Missing header: **401**. No entitlement row, `canceled` or any other
status: **402**. `active`: allowed. `past_due`: allowed while now is no later than `grace_until`, **402** after it or when no grace period is recorded.
Any database error: **503**, never allowed.

## API

| Method | Path | Success | Other answers |
|--|--|--|--|
| GET | `/healthz` | 200 `{"status": "ok"}` | none; no edge check |
| POST | `/v1/usage` | 201 `{"status": "accepted", "event_id"}`, or 200 `{"status": "duplicate", "event_id"}` | 403, 409, 422; edge 401, 402, 503 |
| GET | `/v1/tenants/{tenant_id}/charges?start=&end=` | 200 `{tenant_id, start, end, lines, total_micros}` | 403, 409, 422; edge 401, 402, 503 |
| POST | `/v1/stripe/webhook` | 200 `{"status": "processed" \| "duplicate" \| "stale" \| "ignored"}` | 400, 503; no edge check, the signature is the check |

## Run it

```bash
docker compose up     # Postgres 16, the API on :8000 and the hourly rollup service
scripts/test          # runs the tests against a throwaway Postgres 16 in docker
```

The compose file sets a fake `STRIPE_WEBHOOK_SECRET` (`whsec_test_...`). Settings the code reads:
`DATABASE_URL`, `STRIPE_WEBHOOK_SECRET`, `GRACE_PERIOD_HOURS`. To test against your own database:
`DATABASE_URL=postgresql://... uv run pytest -q`.

## Not built

- Reconciliation of estimated against actual usage.
- Pushing usage to Stripe, invoices, or any call to Stripe.
- Real authentication: the tenant comes from the `X-Tenant-Id` header, which anyone can send.
- A queue or stream in front of the log: events are written straight to Postgres.
- Private deployments that cannot report to a central service.
- A query API over rollups, and an API to add prices (prices are added with `add_price_version`).
- Token columns wider than BIGINT: one tenant, model and hour that sum past 9.2e18 tokens stops the rollup
  loudly (known limit in [docs/ingest.md](docs/ingest.md)).

See also [docs/build-log.md](docs/build-log.md).
