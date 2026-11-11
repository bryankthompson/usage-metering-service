# usage-metering-service

## Start here

Usage metering and billing for AI inference traffic. Tenants report token counts per model; the service stores each event
once, totals them per hour, prices them in integer micro-USD, and gates access on a Stripe test-mode subscription.
To try it: `docker compose up -d --build`, then `scripts/demo.sh`; `scripts/test` runs the tests. Or `scripts/up`: one command that starts the stack, plays a three-tenant scenario against a stand-in inference server and checks the charges independently ([docs/demo.md](docs/demo.md)).
Read next: [how it was built and checked](docs/how-it-was-built.md), [the review reports](docs/reviews/README.md),
and [what is not built](#not-built).

A small prototype. Python 3.12, FastAPI, plain SQL through psycopg, Postgres 16. Test mode only: nothing calls Stripe.

## How it works

**1. A usage event comes in.** `POST /v1/usage` (`app/ingest.py`) takes `tenant_id`, `idempotency_key`,
`model` (1 to 200 characters each), `tokens_in`, `tokens_out` (integers, 0 to 10^12) and `occurred_at`
(ISO 8601 text with a time zone).
One `INSERT ... ON CONFLICT (tenant_id, idempotency_key) DO NOTHING` does the dedupe, so two identical
requests at the same instant store one event. New event: **201** `accepted`. Resend with the same key and
payload: **200** `duplicate`, nothing stored. Both bodies carry `event_id` and `late`. Same key with a
different payload: **409**. Body `tenant_id` is not the
tenant the edge admitted: **403**. Invalid body: **422**. A body over 64 KiB: **413**: an oversized
`Content-Length` is rejected immediately; otherwise streamed chunks are counted and the request is rejected as soon as
they pass the cap, without buffering the rest (also with no credentials). A body the server cannot parse at all (for
example JSON nested thousands deep): **400**.

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
`--every` loop retries it each cycle. Details and the known limits: [docs/ingest.md](docs/ingest.md).

**4. Versioned prices in micro-USD.** `app/pricing.py`. Each model has numbered price versions with an
`effective_from` and two integer prices, micro-USD per 1,000 input and per 1,000 output tokens (1 USD =
1,000,000). A new price is a new version; a database trigger rejects changing or deleting a stored one.
`price_at` picks the version in force at a time, and raises `NoPrice` when there is none: a missing price
is never rated as zero. A rate is `(tokens_in * input_price + tokens_out * output_price) / 1000` in integer
arithmetic, rounded half up once. `GET /v1/tenants/{tenant_id}/charges` rates each hourly rollup row at the price in
force at the start of its hour, so a price change applies from the first hour that starts at or after its effective time
(a version effective at 10:30 applies from the 11:00 hour). Nothing stores a bill. Charges cover closed hours only:
`unclosed_hours` in the answer lists the hours in the range that hold usage but are not closed yet (the earliest 100), so a
total with a non-empty list is partial (100 entries means at least 100). Use `start` and `end` on UTC hour boundaries: an
hour is in or out of a range by its start, so a range that cuts an hour includes or drops all of it. **403** for another tenant, **422** for a naive timestamp, a `start` or `end` that is
not ISO 8601 text, `start >= end`, or a range wider than 366 days, **409** when a row has no price in force (the message
names the models without one, with the first hour of each; past 20 it says how many more). Details: [docs/billing.md](docs/billing.md).

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
  failure and a delete of the stored subscription, are in [docs/billing.md](docs/billing.md)). Unknown tenants and unhandled event types
  are `ignored`. Past the signature check every answer is 200 `{"status": ...}`, except a malformed event
  (**400**), a body over 1 MiB (**413**) and a database failure (**503**; Stripe retries it).

**6. A fail-closed edge.** `require_entitlement` (`app/edge.py`) guards `/v1/usage` and the charges route.
The tenant is the `X-Tenant-Id` header. Missing header: **401**. No entitlement row, `canceled` or any other
status: **402**. `active`: allowed. `past_due`: allowed while now is no later than `grace_until`, **402** after it or when no grace period is recorded.
Any error reading the entitlement: **503**, never allowed. A database failure later in a request is **503** too (one handler for `psycopg.OperationalError` in `app/main.py`), with `Retry-After: 1`. So are
a wait of more than 2 seconds for a lock (resend: nothing of the failed attempt is stored), a connection that takes more than 5 s to open and a
statement that runs more than 15 s on the request path (the edge's own 503, "entitlement check unavailable", carries no `Retry-After`); the
rollup, reconcile and delinquent runs have no statement timeout.

**7. Two read-only reports.** `python -m app.reconcile` recomputes every closed hour from the raw log with its own SQL and
exits 0 when the stored rollups agree, 1 on drift (including a rollup row whose hour is not closed), 2 on bad input;
`--actuals FILE.jsonl` also compares them with totals from another source. It always prints what is not billed (late
events, events over 2 hours past their hour and still not closed, events dated over 1 hour ahead); `--fail-on-unbilled`
makes any of them exit 1, `--json` prints one object ([docs/reconcile.md](docs/reconcile.md)). `python -m app.delinquent` lists `past_due` and `canceled` tenants
with their grace state (`--json`, `--fail-on-expired` for a cron job, `--at ISO`) using the edge's own grace rule
([docs/billing.md](docs/billing.md)).

**8. An optional ClickHouse copy.** With `CLICKHOUSE_URL` set, each closed hour's rollup rows are also written to ClickHouse
(`docker compose --profile analytics`); a failure there is logged and never touches the bill, `python -m app.mirror --backfill`
recovers it, and `--export-actuals` feeds `app.reconcile --actuals` ([docs/clickhouse.md](docs/clickhouse.md)). It is a derived copy, not the bill.

**8. A platform adapter, not run against the real service.** `app/seekrflow.py` turns the `usage` object of an inference
response (chat, embeddings or the final stream chunk) into one usage event, with the response id as the idempotency key; a
missing usage raises and is never counted as zero. It was built from the public API documentation and tested with
hand-written fixtures, the real app and a local stand-in server. No call to the real service has been made: there is no key.
[docs/seekrflow.md](docs/seekrflow.md) lists what was and was not verified.

## API

| Method | Path | Success | Other answers |
|--|--|--|--|
| GET | `/healthz` | 200 `{"status": "ok"}` while the process is up (it says nothing about the database) | none; no edge check |
| POST | `/v1/usage` | 201 `{"status": "accepted", "event_id", "late"}`, or 200 `{"status": "duplicate", "event_id", "late"}` | 400, 403, 409, 413, 422, 503; edge 401, 402, 503 |
| GET | `/v1/tenants/{tenant_id}/charges?start=&end=` | 200 `{tenant_id, start, end, lines, total_micros, unclosed_hours}` | 403, 409, 422, 503; edge 401, 402, 503 |
| POST | `/v1/stripe/webhook` | 200 `{"status": "processed" \| "duplicate" \| "stale" \| "ignored"}` | 400, 413, 503; no edge check, the signature is the check |

Any route: **404** for an unknown path and **405** for a wrong method.

## Run it

```bash
docker compose up -d --build   # Postgres 16, the API on 127.0.0.1:8000 and the hourly rollup service
scripts/demo.sh                # an end-to-end run against that stack (needs curl and python3)
scripts/test                   # runs the tests against a Postgres 16 container it starts in docker
```

The API and database ports listen on 127.0.0.1 only (`API_PORT` and `DB_PORT` change them); the container runs as an
unprivileged user. If something fails while it runs, start at [docs/runbook.md](docs/runbook.md). The demo prices a
model, sends a locally signed subscription event, posts usage (with a resend), closes the hour with the rollup
and checks the charge; it ends with `demo ok`. Details: [docs/demo.md](docs/demo.md). Two things to know:
- Give the API a few seconds after `up -d`. A demo run before it listens prints `FAIL step 3 ... got 000`:
  run it again.
- Postgres applies the files in `db/` only when its data volume is first created, so an existing or
  half-built volume (for example after an interrupted first start, or after an upgrade that adds a column) is not
  repaired by `up`. Every file in `db/` is repeatable and the image carries them, so apply them to the running stack
  without losing data: `docker compose exec -T api python -c "from app import db; c=db.connect(); db.apply_schema(c); print('applied')"`.
  The data lives in the named volume `pgdata`, so `docker compose down` and a recreated container keep it, and only
  `docker compose down -v` resets it (it deletes it). A stack started before the volume was named keeps its old data in an
  anonymous volume: dump it with `pg_dump --data-only` before switching ([docs/demo.md](docs/demo.md)).

The compose file sets a fake `STRIPE_WEBHOOK_SECRET` (`whsec_test_...`). Settings the code reads:
`DATABASE_URL`, `STRIPE_WEBHOOK_SECRET`, `GRACE_PERIOD_HOURS` and `LOG_LEVEL` (default `INFO`; an invalid value falls back to it). To test against your own database:
`DATABASE_URL=postgresql://... uv run pytest -q`. `scripts/test` starts one shared container, `metering-test-db`,
on 127.0.0.1:5455 and leaves it running; `docker rm -f metering-test-db` removes it.
How it is tested, with a burst check and a database outage check you can run on a live stack, and what is not covered: [docs/testing.md](docs/testing.md).
A reading guide to the code, file by file: [docs/code-tour.md](docs/code-tour.md).

## Not built

- Correcting drift: `app.reconcile` reports a difference and changes nothing. (`app.mirror --backfill` only re-copies the optional ClickHouse mirror.)
- ClickHouse at scale or in production: the mirror is a small optional copy (`docs/clickhouse.md`), run by hand on one local node.
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
- Token columns wider than BIGINT: one tenant, model and hour that sum past 9.2e18 tokens fails that hour's close
  loudly and the hour stays unclosed (known limit in [docs/ingest.md](docs/ingest.md)).
- A database that freezes after a connection was made (a paused container) is not bounded by the timeouts, and neither
  is the first query after connecting (known limit in [docs/ingest.md](docs/ingest.md)).
- Bounding the hour of an event: it is the client's `occurred_at`, and `received_at` (stored) is not used to limit it. A client
  can date usage into an hour that is already closed (stored `late`, never billed) or far ahead (its hour cannot close until
  it has ended, so it is not charged until then). `app.reconcile` reports both under `not billed:`; nothing refuses them.
- A close per tenant: hours close for every tenant at once, so one tenant's event in an old quiet hour makes that hour close,
  and another tenant's event for the same hour that arrives afterwards is `late` (stored, never billed).
- Usage after access is cut: once the edge answers 402, a tenant cannot send usage it had not sent yet, because `/v1/usage`
  sits behind the edge; the events are refused, not stored for later.

## How it was built

Claude Code sessions built this in one afternoon and evening, each in its own cockpit (a Zellij session with a lead tab, a
review desk and a merge gate): one lead, two assistant devs, a standing advisor that the lead asked for second opinions,
a review desk (DRC) and short-lived lanes for single jobs. [docs/build-log.md](docs/build-log.md) is the timestamped
record; this is the short version.

[docs/how-it-was-built.md](docs/how-it-was-built.md) explains how AI-written code was checked: the loop each lane was
briefed to follow, the review rounds with their counts, what went wrong and the limits of the method.

**Roles.** The lead wrote the scaffold (schema split by owner, stub routers, a deny-all edge, compose, CI), one brief per
dev, and was the only session allowed to merge. Dev A built usage intake, rollups and the end-to-end test and demo. Dev B
built the price table, the Stripe webhooks, the entitlement record, the edge check and the first README. Later work came
in as lanes, each a branch with its own worktree: reconcile, the delinquent report, the platform adapter, the design
notes, the code tour, the runbook, the testing guide, the hardening fixes and four red-team reviews.

**Gates.** A lane merged only when its branch contained the latest `main` with the tests run on that code, an
independent review desk (DRC) had reviewed it, and the CI check `test` was green. The cockpit's merge
command refuses without a review receipt for the branch, a head that contains the latest `main`, and green CI; commits
added after the review (a merge of `main`, a small fix) and any finding left owed need the lead's written note. Each merge moved `main` and made the next
branch stale, so the lead rebased the early lanes and merged `main` into the later ones, re-ran the tests and merged one
lane at a time, each as its own pull request (`git log --first-parent --merges` lists them).

**Hardening.** The lead ran adversarial agents in rounds: four read-only red-team lanes (money, concurrency, security,
operations), each told to break the code on its own lens and to count a finding only with a command or a failing test
that shows it; a second round of the same four; a narrow third pass on one fix; and a fifth lane that checked every
sentence of the README and docs against the code from a clean clone. Round one found 0 blockers, 9 major and 10 minor. Seven of the majors changed code: a bill that names
every unpriced model, a same-second event that could undo a cancel, a held lock that hung the whole API (now a 503), a paused
database that hung it too (connecting now ends in a 503 after 5 s; a freeze after connecting still hangs, see
[docs/ingest.md](docs/ingest.md)), a request body buffered before the 401 (now capped), and one poisoned rollup hour that
blocked every later hour; two became documented behaviour (how a price change and a back-dated price apply). Round two re-ran each
lane's own repros against the fixed code and attacked the fixes: 0 blockers reported (the operations report gives no count), 3 major, 7 minor. Two majors became code
changes (a cancel older than an applied payment failure was lost; the database lives in a named volume now, because
`docker compose down` followed by `up` had started an empty one); one is written down with its bound in
[docs/ingest.md](docs/ingest.md) (a database that freezes after the connection was made). The review reports are in
[docs/reviews/](docs/reviews/README.md), as written at the time.

**What the gates and the cross-checks caught.**
- DRC found that the hourly token sum can overflow a BIGINT column (now one failed hour that the others survive, and a
  documented limit), a way around the body cap behind a path prefix, and folded fixes of its own into several lanes.
- Reading each other's code, the devs found further defects. Dev A found that every repeated payment-failure webhook
  pushed the grace window forward, which the DRC review of that lane had not flagged (fixed before merge). Dev B found
  a test that could pass by accident because lock waits were counted cluster-wide, and that INFO log lines never reached
  `docker compose logs` because nothing configured logging.
- In the third DRC pass over the entitlements lane, the reviewer folded in a fix of its own and then flagged that same
  fix as opening a fail-open case. The lead asked the advisor, which agreed the guard bought nothing inside
  the one-subscription-per-tenant assumption, and reverted it; the assumption is documented. The advisor also noticed
  that compose published a port next to a public dev secret, which became a loopback-only binding.
- The advisor found that a documented limit was wrong (a cancel for a stored row whose tenant id the new id rule refuses was
  ignored, and a lost cancel is never retried). The cancel fix had its test written first and watched failing.
  DRC folded the daylight-saving fix without a test; the lead added a New York-session test and removed the
  fix once to watch it fail.
- Slips were caught and corrected on every side: the lead mistyped a commit hash in a review job, and named a base that a
  lane did not contain; dev A force-pushed over a merge commit of the lead's, reported eight clean starts of a compose
  change where the true count was seven, and pasted demo output from before its last commit (the lead re-ran the demo
  itself); dev B started throwaway database containers on volumes of live test databases while looking for its own
  (it reported this itself; reads afterwards found no damage; [docs/build-log.md](docs/build-log.md) records it at 19:29).

**Where it hurt.** The review desk could not run the tests until a tracked `scripts/test` gave it a database; messages
to one dev were silently held because the cockpit treats a checkout on a `lane/*` branch as a lane, which cannot read
them; and the review desk takes one job at a time, so with a dozen branches waiting the queue, not the writing, set the pace.
