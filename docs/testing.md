# How this code was tested

Most of this service was written by AI coding sessions, so the question is how anyone knows it works. The answer here is
layers that fail in different ways, then a round where each author attacked the other's code. Everything below can be
re-run or read in the repo; where a claim lives only in a session handoff (not stored here), it says so.

## 1. The layers, and the command for each

| Layer | Command | Passes when | In CI |
|--|--|--|--|
| Tests, real Postgres 16 | `scripts/test` | the last line reads `N passed` (read N there; this file does not copy it) | yes: `uv run pytest -q` |
| One file | `scripts/test tests/test_attack_ingest.py` | same | (part of the above) |
| End to end demo | `docker compose up -d --build`, then `scripts/demo.sh` | prints `demo ok` (price, signed webhook, usage, rollup, charges) | no |
| Reconcile | `docker compose exec -T rollup python -m app.reconcile` | exit 0; 1 = drift, 2 = bad argument (docs/reconcile.md) | no |
| Burst | `python3 scripts/flood.py` | 300 x 201, 300 x 200, no new traceback (600 requests, 150 clients) | no |
| Database outage | `scripts/outage-check.sh` | 503 while the db is stopped, 201 after, earlier row still there | no |

`scripts/test` starts a throwaway Postgres container and gives each checkout its own database (or uses `DATABASE_URL`).
The last three rows need a live `docker compose up`; `API_PORT` picks the port, `COMPOSE_PROJECT_NAME` the project.
CI (`.github/workflows/ci.yml`) runs only the pytest line, on every pull request and push to `main`. The demo, flood and
outage scripts are run by hand and are not in CI.

## 2. What the first round of tests and the review missed

Each slice had its own tests and a separate read-only model review before merge ("DRC" in `docs/build-log.md`).
Then each dev attacked the other's slice. These were found after both, one line each (file, what, how it showed):

| File | Missed defect |
|--|--|
| `app/webhooks.py` | a repeated payment-failure event pushed the grace window forward, so a tenant stayed allowed: found by a dev reading the code, not flagged by the review (build-log 16:01) |
| `docs/build-log.md` 16:05-16:20 | the review's own fix for that opened a fail-open case under out-of-order events; the lead reverted it and documented the assumption |
| `tests/test_rollup.py` | a lock-wait helper counted waiters cluster-wide, so a test could pass by accident; now scoped to one database |
| `scripts/demo.sh` | step 6 checked database-wide rollup counts, so it failed when another tenant's hour closed in the same run |
| `app/ingest.py` | `model` and `tenant_id` had no length cap: one stored event made the rollup fail on the index key size, and no later hour closed |
| `app/ingest.py` | a NUL in a text field, an offset that put the UTC instant outside years 1 to 9999, and a `NaN`, `Infinity` or lone surrogate in a 422 echo were all 500s (the year case also broke the resend and the rollup) |
| `app/ingest.py` | a number or digit string as `occurred_at` was read as Unix seconds (`5` became 1970), billed in the wrong hour with a 201 |
| `app/ingest.py` | a newline in `idempotency_key` forged a log line; a database lost after the edge check was a 500, not 503 |
| `app/ingest.py` | the connection was held while a request waited for a worker: 600 requests from 150 clients gave 503s. No unit test could see it; `scripts/flood.py` does |
| `app/webhooks.py` | a validly signed event with an unusable shape (wrong types, NUL, missing parts) was a 500, which Stripe retries for days |
| `app/webhooks.py` | a canceled subscription kept its grace window and a later `past_due` revived it; a blank webhook secret answered 400, not 503 |
| `app/pricing.py` | `add_price_version` silently rounded a float price; the charges route ran one price query per rollup row (10,000 rows took 3.4 s) |
| `app/edge.py` | a non-ASCII tenant header arrived as mojibake, so that tenant was never admitted |

The review desk is a separate read-only model, not a person. Only the lead session merged, and only with a review of the
exact head and a green CI (README, "Gates"); the repo records no human approval of a merge.

## 3. What passed and is kept as a guard

These were attacked and held; the test stays because the failure would be silent. All run in `scripts/test`.
- Duplicate and conflicting intake: 20 identical requests store one event; 20 with two payloads for one key store one and
  agree on the winner (`tests/test_attack_ingest.py`).
- Ingest racing the hour close, 12 rounds with threads on separate connections: each event is in the closed rollup or
  flagged late, never both, never neither. Rollups run four at once close each hour once.
- A close whose connection is terminated before commit leaves the hour open; a rerun closes it with the same numbers.
- UTC hour boundaries: 23:59:59.999999Z, 00:00:00Z, half-hour and +14:00 offsets, both US DST changes.
- Header tenant against body tenant through the real edge (no override): 403 both ways, a refused key is not used up.
- A rollup sum past BIGINT raises, rolls back and leaves the hour open, as `docs/ingest.md` says.
- Webhook signature tolerance at the edge of the window, concurrent delivery of one event id, grace boundaries, charges
  at every price boundary (`tests/test_attack_webhooks.py`, `tests/test_attack_billing.py`).
- The demo twice on one stack (the second run picks the previous hour); `docker compose up` from nothing.

## 4. How a bug was proven

- **Fails without the fix.** Check out the commit before a fix for the file, then run the new tests:
  `git checkout <fix-commit>^ -- app/ingest.py && scripts/test tests/test_attack_ingest.py`, then
  `git checkout HEAD -- app/ingest.py`. The commit that added `scripts/flood.py` records its own result: on `app/ingest.py`
  as it was on `main` before the attack round it gave 159 x 500 and 10 x 503; with the fixes, 300 x 201 and 300 x 200.
- **Mutation checks.** The two devs report one-line mutations (remove the hour lock, move the commit before the aggregate,
  flip a boundary comparison) that turned the guarding tests red. Those runs are in the handoffs, not in the repo; to
  repeat one, make the edit, run the test, expect red, revert.
- Nothing here was checked by a person reading the diff; the evidence is the runs above.

## 5. What the suite does not cover

- A live burst and a real Postgres restart: only `scripts/flood.py` and `scripts/outage-check.sh`, by hand. Restart policy
  was checked by hand once: stopping postgres inside the db container restarts it (about 9 s to healthy); `docker stop`
  and `docker kill` count as manual stops and are not restarted.
- Multi-process concurrency: the race tests use threads in one process. Two rollup processes at once were not run.
- Stripe: signatures are made locally with a fake secret; no call to Stripe, no real platform call, test mode only.
- Body size on `/v1/usage` for an admitted tenant (the webhook route has a cap) and load beyond the flood's 600 requests.
- Time: the edge clock is replaced in tests; nothing sleeps through a real grace period.

| Known limit (found, not fixed) | Where it is written down |
|--|--|
| The rollup sum is BIGINT: past 9.2e18 tokens one hour fails loudly and blocks later hours | `docs/ingest.md` Known limit; README Not built |
| `TRUNCATE` and the table owner bypass the append-only trigger | `docs/ingest.md` line 3; README section 2 |
| An hour with events closes for every tenant once it is 5 minutes old, so one tenant's event in an old quiet hour makes other tenants' later events for it late (stored, never billed) | here only (late events: `docs/ingest.md`) |
| No real authentication; the first of repeated `X-Tenant-Id` headers wins | README Not built; `app/edge.py` docstring |
| A webhook can store a tenant id the edge never admits (leading space or tab, control characters), and nothing binds a tenant to one customer id | here only (one customer per tenant: `docs/billing.md`) |
| A far-future Stripe `created` could pin `last_event_at` and make real events `stale` (by reading, not tested) | here only |
| An interrupted first `up` leaves a half-built data volume | `docs/demo.md` |
