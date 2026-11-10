# Billing: price table and rating

Money is an integer count of micro-USD (1 USD = 1,000,000). No float and no true division in the money path.

## Prices
- `price_versions` holds, per model, numbered versions (1, 2, ...) with an `effective_from` and two integer
  prices: micro-USD per 1,000 input tokens and per 1,000 output tokens.
- `add_price_version` always adds the next version. A stored version is never changed or removed: a database
  trigger rejects UPDATE and DELETE.
- `price_at(model, at)` returns the version with the greatest `effective_from <= at` (a tie goes to the later
  version). No version in force raises `NoPrice`; a missing price is never rated as zero.

## Rating
`rate` = `(tokens_in * input_price + tokens_out * output_price)` divided by 1,000 in integer arithmetic, rounded
half up ONCE on that sum. Example: 1 token at 1,500 micros per 1k is 1.5, which becomes 2.

## Charges
`GET /v1/tenants/{tenant_id}/charges?start=...&end=...` (timezone-aware datetimes) rates the tenant's
`usage_rollups_hourly` rows with `start <= hour_start < end`. Each row is rated at its own `hour_start`, so a price
change mid-period applies from its effective time. Each row is rounded on its own; `total_micros` is the sum of lines.
Status codes: 403 if the caller's tenant is not the path tenant, 422 for naive datetimes or `start >= end`,
409 if a row has no price in force (the response names the model).

## Tests
`DATABASE_URL=... uv run pytest -q tests/test_pricing.py tests/test_pricing_charges.py` (needs Postgres, see `compose.yaml`).

## Not done
No admin API: prices are added through `add_price_version`. Nothing is sent to Stripe and no invoice is made.

# Billing: Stripe webhooks and the edge check (test mode)

`POST /v1/stripe/webhook` checks `Stripe-Signature` on the raw body (`stripe.Webhook.construct_event`, secret
`STRIPE_WEBHOOK_SECRET`) before it touches the database. No secret set: 503. Missing or bad signature, a timestamp
older than 5 minutes, or an unparsable body: 400. The tenant is `subscription.metadata.tenant_id` (the row is
created or updated) or, for invoices, the `entitlements` row with that `stripe_customer_id`. No tenant found: 200
`{"status": "ignored"}`, logged, not recorded. Other event types are ignored the same way.

| Event | Effect |
|---|---|
| `customer.subscription.created/updated/deleted` | `plan` = Stripe price id. `active`/`trialing` -> `active`, clears the failure and grace. `past_due` -> `past_due`, keeps an existing grace window. Any other status -> `canceled`. |
| `invoice.payment_failed` | `past_due`, `payment_failed_at` = event `created`, `grace_until` = that + `GRACE_PERIOD_HOURS` (default 72). A `canceled` tenant stays canceled. |

Each event id is recorded in `stripe_events` in the same transaction as the change; a redelivery is 200 `duplicate`.
An event older than `last_event_at` is recorded but not applied (200 `stale`); equal timestamps apply in arrival order.

The edge (`require_entitlement`) reads tenant from the `X-Tenant-Id` header, a stand-in for real authentication.

| Case | Answer |
|---|---|
| header missing | 401 |
| no row, `canceled`, unknown status | 402 |
| `active` | allowed |
| `past_due` | allowed while `now <= grace_until`; 402 after, or with no `grace_until` |
| database error | 503, never allowed |

Tests: `uv run pytest -q tests/test_webhooks.py tests/test_edge.py` (signed with a fake `whsec_test_...` secret).
Not done: calling Stripe, usage push, invoices, real authentication.
