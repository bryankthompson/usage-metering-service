# Design notes: what is built, where more would attach, what was cut

Nothing in the sections "Where things would attach" and "A day against a week" is built. Each item is marked NOT BUILT
and tied to the line in the existing code where it would attach. The "estimate" sizes are guesses, not measurements.

## 1. What is built, and the one rule behind it

The rule: write the fact down first, derive everything else from it, and when unsure, deny.

- **Append-only log first.** `usage_events` rejects row UPDATE and DELETE by trigger (`db/001_usage.sql:20-29`, function `usage_events_append_only`).
- **Rollups derived.** `AGGREGATE_HOUR` sums the log per tenant, model and hour (`app/rollup.py:30-37`) and `app.reconcile`
  recomputes it with its own SQL to check it (`docs/reconcile.md`).
- **Prices versioned.** A stored price version cannot change; a new price is a new version (`db/002_billing.sql:14-23`, function `price_versions_immutable`;
  `add_price_version`, `app/pricing.py:55-56`). Money is integer micro-USD (`docs/billing.md`).
- **The gate fails closed.** Any entitlement that is missing, not active, past grace or unreadable denies (`require_entitlement`, `app/edge.py:52-64`).

## 2. Where things would attach (NOT BUILT)

**A queue or stream in front of ingest.** The seam is the single `INSERT ... ON CONFLICT (tenant_id, idempotency_key) DO NOTHING`
(`INSERT_EVENT`, `app/ingest.py:59-66`). A consumer would call the same insert, so a redelivery is a duplicate, not a second event. What
changes: `POST /v1/usage` would publish and answer once the broker has the message, so a 201 would mean "queued", not "stored"
(today 201 means committed, `_store`, `app/ingest.py:139-144`), and the `late` flag could not be returned to the caller. What stays: the log,
the shared hour lock taken by the consumer (`lock_hour_shared`, `app/ingest.py:141`) and the rollup. The 422 checks (`tenant_id`, `idempotency_key` and `model` capped at 200 characters, `MAX_TEXT` at `app/ingest.py:53` and the `Text` type at `:81`) would have to
run at the producer or the consumer. Failure added: a message the consumer cannot
insert (for example a 409 payload mismatch) needs a dead-letter place; today the caller sees it.

**A columnar store for rollups at scale.** The seam is `AGGREGATE_HOUR` and the two rollup tables (`app/rollup.py:30-37`,
`db/001_usage.sql`). After `close_hour` commits (`conn.commit()`, `app/rollup.py:83`), the same rows would be written to the columnar
store (a dual write). What stays: the log remains the source and the Postgres rollup remains the bill. Comparison: export the
columnar totals as the JSON lines file `app.reconcile --actuals` already reads (`docs/reconcile.md`); drift above tolerance exits 1.
Failure added: the second write can fail after the first commits, so it needs its own retry and the reconcile run is the detector.

**Containers and a cluster.** In `compose.yaml`: `db` is the Postgres 16 container with a `pg_isready` health check and
`restart: unless-stopped` (`compose.yaml:15-20`); `api` is `uvicorn`, waits for a healthy `db` and has `restart: unless-stopped`
(`compose.yaml:22-34`); `rollup` is `python -m app.rollup --every 3600` with `restart: unless-stopped` (`compose.yaml:36-44`).
Only `db` has a health check: `api` has none although the app has `GET /healthz` (`app/main.py:49-55`). The API answers 503 on
every route when the database is unreachable (`app/main.py:39-47`) and 413 to a webhook body over 1 MiB
(`app/webhooks.py:26,38`); both matter to whatever a cluster uses for probes and load balancing. `app.*` log lines show at
`LOG_LEVEL` (default INFO) through `configure_logging` (`app/main.py:17-30`). A cluster would add liveness and readiness probes
on `/healthz` (which does not touch the database), a CronJob or equivalent for the rollup instead of a sleeping loop, and
secrets in place of the fake `STRIPE_WEBHOOK_SECRET` in the file. No manifests are written and none are tested.

**Private deployments that cannot report to a central service.** A site-side spool would write usage events as JSON lines in the
same shape as the `POST /v1/usage` body, with the same idempotency keys. The file is carried out of the boundary and imported
through the same insert, so importing it twice is safe (replay-safe by the key). What stays: everything after the insert.
The limit: an event for an hour that is already closed here is stored and flagged `late = true` and is never billed
(the `EXISTS (SELECT 1 FROM closed_hours ...)` in `INSERT_EVENT`, `app/ingest.py:62-63`; `docs/ingest.md`). A real design needs a late-adjustment rule, such as a credit or debit line on a later
invoice. That rule is NOT designed.

**Pushing usage to a payment provider.** The seam is the closed rollup rows. It needs a table of pushed hours, an idempotency
identifier per tenant and hour (so a retried push is not a second charge), and a retry rule for failures. Nothing calls the
provider today (`README.md`, "Not built").

## 3. Decisions and why

| Decision | Why | Cost |
|--|--|--|
| Advisory lock per hour instead of a queue | Ingest holds a shared lock, close takes the exclusive one, so each event is in the closed rollup or flagged late, never both and never neither (`hour_index` and the exclusive lock in `close_hour`, `app/rollup.py:49-51,74-78`; the shared lock in `_store`, `app/ingest.py:139-141`). The lead answered the dev's design question this way (`docs/build-log.md`, 15:27). | Ingest and close share one Postgres; a close waits for in-flight inserts. |
| Late events flagged and never billed, instead of reopening a closed hour | A closed hour never changes, so a bill already produced stays true (`docs/ingest.md`, "Late events"). | Usage for a closed hour is lost to billing until a late-adjustment rule exists. |
| Grace counted from the first failure that arrives | Stripe retries one failed invoice for days; letting each retry move the window kept a tenant allowed through the whole retry period (found in cross-review, `docs/build-log.md`, 16:01; `docs/billing.md`). | A new window starts only after an `active` subscription event clears the old one; `invoice.paid` does not (`docs/billing.md`). It is Stripe's first failure only when delivery order equals creation order. |
| One subscription per tenant assumed | A guard for two subscriptions bought nothing inside that assumption and opened a fail-open case under out-of-order events (`docs/build-log.md`, 16:20). | Deleting either of two subscriptions denies the tenant (`docs/billing.md`). |
| Unknown Stripe status means canceled | Only `active`, `trialing` and `past_due` grant access; unknown means deny (`STATUS_MAP` and its lookup, `app/webhooks.py:22-23,137-139`; `docs/build-log.md`, 15:32). | A new Stripe status that should grant access denies until the map is changed. |
| Prices as immutable versions | A past charge can be re-rated at the price in force then; a trigger enforces it (`price_versions_immutable`, `db/002_billing.sql:14-23`). | A wrong price is fixed by a new version, not an edit; no admin API exists. |
| The edge reads the entitlement on its own connection | A database that is down fails at the edge as 503 and never lets the request through (`require_entitlement`, `app/edge.py:39-40,52-57`); a handler in `app/main.py:39-47` now also answers 503 for a database that dies after the edge check. | One extra connection per request. |
| Hand-run live checks instead of a CI stack | The demo and the compose runs were done by hand on throwaway projects (`docs/build-log.md`, 16:23, 16:34); CI runs only the tests. The log gives no further reason. | Compose and the demo are not checked on every merge. |

## 4. A day against a week

The first working version took one afternoon: the log shows the scaffold at 15:08 and the last core slice merged at 16:20, with
the README and hardening after that (`docs/build-log.md`). It contains intake, rollups, versioned prices, webhooks, the edge, compose,
the demo and, later, reconcile. A further week would buy the items below. Sizes are estimates.

| Item | Why it is not here | Size in days (estimate) | The test that would show it worked |
|--|--|--|--|
| Real authentication | The tenant is the `X-Tenant-Id` header; identity needs a decision on who issues credentials. | 2 to 3 | A request with a forged tenant header and a valid credential for another tenant gets 401 or 403 and stores nothing. |
| Late adjustments | The rule (credit or debit on a later invoice) is not designed. | 3 to 4 | A late event for a closed hour produces one adjustment line on the next period's charges, once, and a resend adds none. |
| Payment-provider push | Test mode only; needs the pushed-hours table and retry rule. | 3 to 5 | A provider that fails the first call and succeeds the second receives one usage record per hour, identified by its idempotency identifier. |
| A queue | No load figure exists that shows Postgres alone is not enough. | 3 to 4 | Publish the same event twice and kill the consumer mid-batch: the log holds one row per key. |
| A columnar store | No data volume that needs it. | 4 to 5 | After a dual-written period, `app.reconcile --actuals` against the columnar export exits 0; an altered row exits 1. |
| A cluster deployment | Compose was enough to demo; manifests would be untested guesses. | 3 to 5 | Kill the API and the rollup pod: both come back, the rollup closes the hour once, `/v1/usage` answers again. |
| A usage dashboard | No query API over rollups exists (`README.md`, "Not built"). | 3 to 4 | The dashboard totals for an hour equal `total_micros` from the charges route for the same range. |
