# Red team, lens: concurrency

> Review report as written at the time. File:line citations refer to the SHA named in the report, not to today's tree.

Checkout: origin/main (da520b6). Suite before: 507 passed. Attack scripts (not committed): race_close.py, wh.py, starve.py.

Totals: 0 blockers, 2 major, 1 minor.

## RT-concurrency-1  MAJOR  A same-second event can undo a cancel (stale check uses `<`, ties are applied)
File: app/webhooks.py:123 (`created < row["last_event_at"]`) and :174 (same test in payment_failed)
Stripe `created` has one-second resolution. `customer.subscription.updated` (active) and `customer.subscription.deleted` often share a
second (a cancel right after a plan change). If `deleted` is applied first, an equal-`created` `updated(active)` is not stale, so it
overwrites `canceled` with `active`: access is back, permanently (no later event is coming), with no grace and no log line other than "processed".
Repro (attack script wh.py, section A, real DB, handle_event):
  deleted(created=1000) then updated status=active (created=1000)  ->  outcome 'processed', entitlement status 'active'
Fix: treat a tie as stale when the stored row is canceled and the incoming event is not a delete (or compare (created, rank) with canceled > past_due > active).

## RT-concurrency-2  MAJOR  One held hour lock hangs the whole API, /healthz included (no lock_timeout, shared 40-thread pool)
File: app/rollup.py:57 (`lock_hour_shared`, no timeout) and app/ingest.py:119 (sync handler holds a threadpool thread while waiting)
Ingest waits for the hour's exclusive lock without a timeout. Each waiter pins one of uvicorn's 40 sync worker threads and one DB connection.
The hour is chosen by the client's `occurred_at`, so ~40 requests for the hour being closed (or any hour whose lock is held longer, e.g. a
big close) block every other request: other hours, other tenants, webhooks, healthz. It recovers when the lock is released.
Repro (live compose stack, own project): hold the lock in psql, send 60 ingests for that hour, probe:
  psql: select pg_advisory_lock(1,<hour index of 2024-01-01T10Z>); select pg_sleep(40);
  python3 starve.py <tenant>
  -> /healthz FAIL TimeoutError 10.01
  -> other-hour ingest FAIL TimeoutError 10.01
  (after the lock ended: curl /healthz -> {"status":"ok"})
Same shape for the webhook per-tenant advisory lock (app/webhooks.py:120), not run separately.
Fix: `SET LOCAL lock_timeout = '2s'` before the advisory lock in ingest (map the lock error to 503 + Retry-After; a resend is safe); optionally cap waiters per hour.

## RT-concurrency-3  MINOR  Grace window is computed from the first failure ARRIVED, not the first failure
File: app/webhooks.py:174-179
Docs (billing.md) say a later older failure does not move an existing window, so this is the documented behaviour, but the brief's claim
"grace computed from the first failure" is false under parallel out-of-order delivery: 10 concurrent payment_failed events
(created 2000..2090) gave grace_until = 1970-01-04T00:33:30Z (first arrived = created 2010), expected 00:33:20 (created 2000); 7 of 10 were 'stale'.
Effect: up to a few seconds to minutes of skew with Stripe's retry spacing; correct only if arrival order == created order. Fix: if the older failure arrives
while grace_until is set, move grace_until back to min(grace_until, created + grace) (shrinking only).

## What I tried that held
- Ingest racing close_hour, 30 ingests + 2 closers per round on one hour, 150 rounds (60 more with a mix counter; 55/60 rounds had both late and non-late events, so the race really occurred): 0 violations of "rollup count == non-late events, late + non-late == all".
- Two rollup processes closing the same hour: one wins, the other returns None, no duplicate rollup rows.
- Same webhook event delivered 20 times at once: 1 processed, 19 duplicate.
- payment_failed racing subscription.deleted (60 rounds, deleted newer): always ends canceled; no deadlock.
- Lock ordering between webhook paths (stripe_events insert, tenant advisory lock, entitlement row lock) and ingest/close: no cycle found; advisory key spaces (one bigint vs two int4) do not collide.
- Connection exhaustion: scripts/flood.py 2000 requests / 400 clients: 1000x201, 1000x200, no 503/500, no tracebacks. Pool of 40 threads bounds connections below max_connections=100, so exhaustion by volume does not happen (it happens only through RT-2).
  > Note, 2026-11-11: since PR #67 the request path borrows connections from a bounded per-process pool (`request_connection` in app/db.py), sized by `DB_POOL_MAX` (default 40); a request that waits longer than `DB_POOL_TIMEOUT` seconds (default 5) for a free connection answers 503. The 40 worker threads are no longer what bounds the connections.
- No lock held across a network call (signature check and Stripe-free paths are outside transactions; the edge closes its connection before the handler opens one).

## What I did not get to
- Multiple uvicorn workers / several API replicas (the 40-thread bound no longer holds, so the 100-connection limit could).
- Closing a very large hour (millions of rows) to measure how long ingest stays blocked.
- Killing connections with pg_terminate_backend mid-close (operations lens).
- Webhook lock-starvation repro (only the ingest one was run).

# Round two (origin/main 1abb7b4)

Totals: 0 blockers, 1 major, 2 minor (one new major is an old, still-open case I had not written up in round one).

## Re-run of round one
- RT-concurrency-1 (same-second tie undoes a cancel): FIXED. wh.py section A: `deleted(1000)` then `updated(active, 1000)` -> `stale`, status stays canceled (logs "webhook tie after cancel"). Parallel: deleted + updated(active) + updated(past_due), all created=1000, 100 rounds (attack script tie.py): not canceled in 0 of 100.
- RT-concurrency-2 (held hour lock hangs the API): FIXED. Same repro (lock held 25 s, 60 ingests waiting): /healthz ok 0.0 s, other-hour ingest 201 in 0.02 s; one ingest for the held hour -> `503`, `retry-after: 1`, 0 rows stored for its key. Webhook, tenant lock held (attack script whlock.py): 503 + Retry-After after 2.01 s, `stripe_events` row count 0, retry after release 200, redelivery 200. Both give up after 2 s as designed.
- RT-concurrency-3 (grace from first arrival): CHANGED? No: unchanged (10 parallel failures, grace_until 00:34:00 vs 00:33:20 expected). Still a documented behaviour; MINOR.

## RT-concurrency-4  MAJOR  A cancel whose `created` is older than a payment_failed already applied is stale and lost (not fixed by the tie rule)
File: app/webhooks.py:121 (`created < row["last_event_at"]`), and :182 where payment_failed writes `last_event_at`
Invoice and subscription events share one `last_event_at`. After `payment_failed(created=1001)` is applied, `subscription.deleted(created=1000)` is `stale`,
recorded as handled (so Stripe's redelivery is a duplicate) and the tenant stays `past_due` with the grace window. Access ends only at grace_until (72 h default).
Repro (wh.py, section B, unchanged on 1abb7b4): `B pf processed then deleted(created 1000): stale past_due`
Fix: never let a stale check skip a `deleted` (a delete is monotone: apply it whatever `last_event_at` is), or keep separate `last_event_at` per stream (subscription / invoice).

## Attacks on the fixes (all held unless noted)
- lock_timeout leak: it is `set_config(..., is_local=true)`. attack script leak.py: fresh conn `0`; inside the txn `2s`; after commit `0`; after rollback `0`. The statement_timeout (15 s) is session-level and set at connect, as documented. No leak into another statement or a later transaction. Connections are not reused anyway (one per request, closed).
  > Note, 2026-11-11: connections are now reused: the pool hands the same physical connection to later requests (`request_connection` in app/db.py). `lock_timeout` is still transaction-local, and the `statement_timeout` is set once on each new connection by the pool's `configure` step.
- Half-written after a 503: ingest 503 stores nothing (count 0 above); webhook 503 leaves no `stripe_events` row and a retry processes. Held.
- Spurious 503 under normal load: 20 ingest workers on one hour plus 5 on another while the rollup closed a 4M-row hour (3.4 s for the whole run): 0 x 503, 945 x 201. NOT held for a huge hour: same test with a 12M-row hour (close took 23.8 s): hour being closed -> 1066 x 201, **160 x 503** (13 % of its requests); the other hour 1979 x 201, 0 x 503. All 503s carry Retry-After 1 and are safe to resend, so the effect is bounded but real: a client that does not retry loses that event, and an hour closes slowly in proportion to its size. Invariant after that run: rollup event_count 12,000,823 == non-late events 12,000,823, late 243; no event in both or neither. MINOR (NOTE for sizing: ingest of the closing hour fails closed rather than waiting).
- Per-hour isolation (attack script poison.py, CHECK constraint on usage_rollups_hourly makes hours 1 and 3 fail after the advisory lock is taken; two threads, each with its own connection and close_due_hours, 3 runs): closed hours always {0,2,4,5}, failed {1,3} reported by both, the connection runs `select 1` afterwards, 0 advisory locks left, no duplicate rollup rows, one process does all the closing and the other none. Held.
- unclosed_hours vs concurrent close (attack script unclosed.py, 200 hours of 20,000 events, a close racing 40 charge reads each, 8,000 samples): neither listed nor billed = **0**; listed and billed 188; listed only 139; billed only 7,673. Held.
- Tie rule under parallel delivery: held (0 of 100).

## Round two: what I did not get to
- Charges route still uses the `get_conn` dependency (app/pricing.py:204): a burst of charges calls while the pool is saturated holds that connection while it waits for a thread; not tested.
- Multi-worker uvicorn; two replicas of the rollup service with `--every`.
- The lock_timeout on the rollup side is absent by design; I did not test a stuck ingest transaction (idle in transaction) holding a shared lock and blocking the close indefinitely (no idle_in_transaction_session_timeout).

# Round three (origin/main a89fa21, the terminal-delete rule)

Totals: 0 blockers, 0 major, 2 minor. Scripts: r3.py and r3b.py (real DB, handle_event, threads).

## 1. RT-concurrency-4 repro: FIXED
`payment_failed(1001)` then `deleted(sub_1, 1000)` -> `processed`, status `canceled`, `last_event_at` stays 1001 (max). Parallel, same two events, 100 rounds: canceled 100, other 0.
Deletes that do NOT match the stored id still go by the clock, as designed: no id, integer id `123`, list id, `SUB_1`, ` sub_1` (all five) -> `stale`, tenant stays past_due. So the rule cannot be reached by a spelling trick; a differently spelled id only loses the fast path (it never cancels more than before).

## RT-concurrency-5  MINOR  Replacing a subscription (create the new one, end the old one) cancels a paying tenant, in either arrival order, and flips the stored id back to the old one
File: app/webhooks.py:132-138 (clock test) and :153 (subscription id written from the event)
Documented as the one-subscription assumption, but it is the normal Stripe sequence and the outcome is a permanent lock-out, so worse than the doc line suggests.
Repro (r3b.py, 100 parallel rounds each): `created(sub_2, 1000)` + `deleted(sub_1, 1001)` -> `canceled/sub_1` in 100 of 100 (in order: delete is newer, applies; reversed: the delete goes first and `created(sub_2)` is stale, 2b). Only a later event for sub_2 revives the tenant. With the delete created BEFORE the create (1000 vs 1001): `active/sub_2` in 100 of 100 (correct).
Fix: a `deleted` whose id differs from the stored id (and the stored status is not canceled) should be ignored/stale instead of cancelling, i.e. only the stored subscription's delete is terminal; and let a created/updated for an id the row has never seen win over a canceled row if `created` is later than the cancel's own `created` (keep a separate `canceled_at`).

## RT-concurrency-6  MINOR  `last_event_at` is shared with invoice events, so it can lock out a legitimate re-subscribe made after a cancel
File: app/webhooks.py:132 (stale test) with max() at :155
After `payment_failed(1001)` then terminal `deleted(sub_1, 900)`, `last_event_at` is 1001. A new `created(sub_2, 1000)` (after the cancel at 900, before the failure at 1001) is `stale`; the tenant is paying but stays canceled (r3.py 3c: `stale canceled`, deterministic). Fails closed, ends at the next event with created >= 1001.
Fix: compare a subscription event with the last SUBSCRIPTION event time (or the cancel's time when canceled), not the invoice-inflated value.

## What I tried that held
- Cancel undone by a tie or max(): `updated(sub_1 active)` with created == last (1001) and with an older created (949) after a terminal delete -> `stale`, canceled. Parallel updated(active,1000) + deleted(1000) + payment_failed(1001) x100: canceled 100/100.
- Same delete 20 times in parallel: 1 processed, 19 duplicate, canceled; with a payment_failed first (5b): same, `last_event_at` stays 1001.
- An OLD delete (created 950) of sub_1 after sub_2 is stored: stale, tenant stays active (2c).
- Terminal delete overwriting customer: a signed `deleted(sub_1)` carrying another customer id replaces `stripe_customer_id` (r3.py 4), so a later `payment_failed` for the old customer is `ignored`. That cannot give access (the row is canceled, a failure only denies, and only a subscription event can reopen), so NOTE only; it needs a signed event with a changed customer for the same subscription id.
- No path found to `active` after a matching delete other than a subscription event with a later created.

## What I did not get to
- A stored row with `stripe_subscription_id` NULL (first event had no id): the delete is clock-judged by design; not exercised against a payment_failed race.
