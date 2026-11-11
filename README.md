# usage-metering-service

A small prototype of a usage metering and billing service. Tenants report usage events (a model and
input and output token counts); the service counts each event once, rolls the events up per hour, prices
them in integer micro-USD, and serves only tenants whose Stripe test-mode subscription is in good standing.
Python 3.12, FastAPI, plain SQL through psycopg, Postgres 16. Test mode only: nothing calls Stripe.

## How it works

**1. A usage event comes in.** `POST /v1/usage` (`app/ingest.py`) takes `tenant_id`, `idempotency_key`,
`model` (1 to 200 characters each), `tokens_in`, `tokens_out` (integers, 0 to 10^12) and `occurred_at`
(ISO 8601 text with a time zone).
One `INSERT ... ON CONFLICT (tenant_id, idempotency_key) DO NOTHING` does the dedupe, so two identical
requests at the same instant store one event. New event: **201** `accepted`. Resend with the same key and
payload: **200** `duplicate`, nothing stored. Both bodies carry `event_id` and `late`. Same key with a
different payload: **409**. Body `tenant_id` is not the
tenant the edge admitted: **403**. Invalid body: **422**.

**2. Into an append-only log.** Events are stored in `usage_events` (`db/001_usage.sql`); a trigger rejects
every row UPDATE and DELETE (TRUNCATE and a superuser are not stopped). The application never rewrites an
accepted event.

**3. Hourly rollups.** `app/rollup.py` sums one UTC hour of events per tenant and model into
`usage_rollups_hourly` and records the hour in `closed_hours`, in one transaction. `python -m app.rollup`
closes every hour that has events and ended at least 5 minutes ago (`--every SECONDS` repeats; compose runs
it hourly as the `rollup` service). Late and duplicate events never change a closed hour: an event whose
UTC hour is already closed is stored with `late = true` and answered 201 with `"late": true` (a resend says
so too), but it is left out of the rollup, so it is never charged. Ingest takes a shared advisory lock on the event's hour and closing
takes the exclusive one, so every event of a closed hour is either in its closed rollup or flagged late,
never both and never neither. Details and the known limit: [docs/ingest.md](docs/ingest.md).

**4. Versioned prices in micro-USD.** `app/pricing.py`. Each model has numbered price versions with an
`effective_from` and two integer prices, micro-USD per 1,000 input and per 1,000 output tokens (1 USD =
1,000,000). A new price is a new version; a database trigger rejects changing or deleting a stored one.
`price_at` picks the version in force at a time, and raises `NoPrice` when there is none: a missing price
is never rated as zero. A rate is `(tokens_in * input_price + tokens_out * output_price) / 1000` in integer
arithmetic, rounded half up once. `GET /v1/tenants/{tenant_id}/charges` rates each hourly rollup row at the price in
force at the start of its hour, so a price change applies from the first hour that starts at or after its effective time
(a version effective at 10:30 applies from the 11:00 hour). Charges cover closed hours only and nothing stores a bill. **403** for another tenant, **422**
for a naive timestamp, a `start` or `end` that is not ISO 8601 text, or `start >= end`, **409** when a row has no price in force. Details:
[docs/billing.md](docs/billing.md).

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
  older than the last one applied does not overwrite newer state (`stale`; one narrow exception for an older payment
  failure is in [docs/billing.md](docs/billing.md)). Unknown tenants and unhandled event types
  are `ignored`. Past the signature check every answer is 200 `{"status": ...}`, except a malformed event
  (**400**), a body over 1 MiB (**413**) and a database failure (**503**; Stripe retries it).

**6. A fail-closed edge.** `require_entitlement` (`app/edge.py`) guards `/v1/usage` and the charges route.
The tenant is the `X-Tenant-Id` header. Missing header: **401**. No entitlement row, `canceled` or any other
status: **402**. `active`: allowed. `past_due`: allowed while now is no later than `grace_until`, **402** after it or when no grace period is recorded.
Any error reading the entitlement: **503**, never allowed. A database failure later in a request is **503** too (one handler for `psycopg.OperationalError` in `app/main.py`).

**7. Two read-only reports.** `python -m app.reconcile` recomputes every closed hour from the raw log with its own SQL and
exits 0 when the stored rollups agree, 1 on drift, 2 on bad input; `--actuals FILE.jsonl` also compares them with totals from
another source ([docs/reconcile.md](docs/reconcile.md)). `python -m app.delinquent` lists `past_due` and `canceled` tenants
with their grace state (`--json`, `--fail-on-expired` for a cron job, `--at ISO`) using the edge's own grace rule
([docs/billing.md](docs/billing.md)).

**8. A platform adapter, not run against the real service.** `app/seekrflow.py` turns the `usage` object of an inference
response (chat, embeddings or the final stream chunk) into one usage event, with the response id as the idempotency key; a
missing usage raises and is never counted as zero. It was built from the public API documentation and tested with
hand-written fixtures, the real app and a local stand-in server. No call to the real service has been made: there is no key.
[docs/seekrflow.md](docs/seekrflow.md) lists what was and was not verified.

## API

| Method | Path | Success | Other answers |
|--|--|--|--|
| GET | `/healthz` | 200 `{"status": "ok"}` | none; no edge check |
| POST | `/v1/usage` | 201 `{"status": "accepted", "event_id", "late"}`, or 200 `{"status": "duplicate", "event_id", "late"}` | 403, 409, 422; edge 401, 402, 503 |
| GET | `/v1/tenants/{tenant_id}/charges?start=&end=` | 200 `{tenant_id, start, end, lines, total_micros}` | 403, 409, 422; edge 401, 402, 503 |
| POST | `/v1/stripe/webhook` | 200 `{"status": "processed" \| "duplicate" \| "stale" \| "ignored"}` | 400, 413, 503; no edge check, the signature is the check |

## Run it

```bash
docker compose up -d --build   # Postgres 16, the API on 127.0.0.1:8000 and the hourly rollup service
scripts/demo.sh                # an end-to-end run against that stack (needs curl and python3)
scripts/test                   # runs the tests against a Postgres 16 container it starts in docker
```

The API and database ports listen on 127.0.0.1 only (`API_PORT` and `DB_PORT` change them). The demo prices a
model, sends a locally signed subscription event, posts usage (with a resend), closes the hour with the rollup
and checks the charge; it ends with `demo ok`. Details: [docs/demo.md](docs/demo.md). Two things to know:
- Give the API a few seconds after `up -d`. A demo run before it listens prints `FAIL step 3 ... got 000`:
  run it again.
- Postgres applies the files in `db/` only when its data volume is first created, so an existing or
  half-built volume (for example after an interrupted first start, or after an upgrade that adds a column) is not
  repaired by `up`. Every file in `db/` is repeatable and the image carries them, so apply them to the running stack
  without losing data: `docker compose exec -T api python -c "from app import db; c=db.connect(); db.apply_schema(c); print('applied')"`.
  Only `docker compose down -v` resets the data (it deletes it).

The compose file sets a fake `STRIPE_WEBHOOK_SECRET` (`whsec_test_...`). Settings the code reads:
`DATABASE_URL`, `STRIPE_WEBHOOK_SECRET`, `GRACE_PERIOD_HOURS` and `LOG_LEVEL` (default `INFO`; an invalid value falls back to it). To test against your own database:
`DATABASE_URL=postgresql://... uv run pytest -q`. `scripts/test` starts one shared container, `metering-test-db`,
on 127.0.0.1:5455 and leaves it running; `docker rm -f metering-test-db` removes it.
How it is tested, with a burst check and a database outage check you can run on a live stack, and what is not covered: [docs/testing.md](docs/testing.md).

## Not built

- Correcting drift: `app.reconcile` reports a difference and changes nothing.
- Any call to the inference platform the adapter is written for (no key), and its per-model prices: the prices in the tests are illustrative.
- Cluster manifests, a dashboard, late adjustments and the other items listed in [docs/design-notes.md](docs/design-notes.md).
- Pushing usage to Stripe, invoices, or any call to Stripe.
- Real authentication: the tenant comes from the `X-Tenant-Id` header, which anyone can send.
- A test-mode guard: `livemode` is never read, so the service is test-mode only because the compose file sets a
  fake webhook secret.
- `invoice.paid`: paying an invoice does not restore access; only a `customer.subscription.*` event does.
- A queue or stream in front of the log: events are written straight to Postgres.
- Private deployments that cannot report to a central service.
- A query API over rollups, and an API to add prices (prices are added with `add_price_version`).
- Billing late events: they are stored and flagged, never rolled up, and no application code reopens a closed hour.
- Token columns wider than BIGINT: one tenant, model and hour that sum past 9.2e18 tokens stops the rollup
  loudly (known limit in [docs/ingest.md](docs/ingest.md)).

## How it was built

Three Claude Code sessions built this in one afternoon, each in its own cockpit (a Zellij session with a lead tab, a
review desk and a merge gate): one lead and two assistant devs. [docs/build-log.md](docs/build-log.md) is the
timestamped record; this is the short version.

**Roles.** The lead wrote the scaffold (schema split by owner, stub routers, a deny-all edge, compose, CI), one brief
per dev, and was the only session allowed to merge. Dev A built usage intake, rollups and the end-to-end test and demo.
Dev B built the price table, the Stripe webhooks, the entitlement record, the edge check and this README. Work came
in as six lanes (`ingest`, `pricing`, `rollup`, `entitlements`, `e2e`, `readme`), each a branch with its own worktree.

**Gates.** A lane merged only when its branch was rebased onto the latest `main` with the tests run on the rebased
code, an independent review desk (DRC) had reviewed that exact head, and the CI check `test` was green. The cockpit's
merge command refuses without the review receipt for that head, a head that contains the latest `main`, and green CI;
when the reviewer left a finding owed, it also wants the lead's written note on it. Each merge moved `main`, which made
the next branch stale, so the lead rebased, re-tested and re-reviewed one lane at a time.

**What the gates and the cross-checks caught.**
- DRC found that the hourly token sum can overflow a BIGINT column (kept as a documented limit), and folded fixes of
  its own into several lanes.
- Reading each other's code, the devs found further defects. Dev A found that every repeated payment-failure webhook
  pushed the grace window forward, which the DRC review of that lane had not flagged (fixed before merge). Dev B found
  a test that could pass by accident because lock waits were counted cluster-wide.
- In the third DRC pass over the entitlements lane, the reviewer folded in a fix of its own and then flagged that same
  fix as opening a fail-open case. The lead asked a standing advisor lane, which agreed the guard bought nothing inside
  the one-subscription-per-tenant assumption, and reverted it; the assumption is documented. The advisor also noticed
  that compose published a port next to a public dev secret, which became a loopback-only binding.
- Slips were caught and corrected on every side: the lead mistyped a commit hash in a review job; dev A reported eight
  clean starts of a compose change where the true count was seven, and pasted demo output from before its last commit
  (the lead re-ran the demo itself).

**Where it hurt.** The review desk could not run the tests until a tracked `scripts/test` gave it a database; messages
to one dev were silently held because the cockpit treats a checkout on a `lane/*` branch as a lane, which cannot read
them; and the serial review and merge queue idled both devs until the work was split so they could cross-review and
write the README in parallel.
