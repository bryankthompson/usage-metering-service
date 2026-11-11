# Usage intake: `POST /v1/usage`

Stores one usage event in the `usage_events` table (`db/001_usage.sql`; a trigger refuses UPDATE and DELETE, but
not TRUNCATE or the table owner) behind the edge check `require_entitlement`, which reads the entitlement on its own
connection (the handler uses a second one).

Body (all required): `tenant_id`, `idempotency_key` and `model` (1 to 200 chars each, no NUL character), `tokens_in` and `tokens_out` (integers 0 to 10^12), `occurred_at` (ISO 8601 text with a time zone, UTC year 1 to 9999; a number is refused, not read as Unix seconds). The 200-char cap keeps these columns inside Postgres's index key limit: an oversized `model` would be stored and then stop the rollup from closing any later hour.

| Status | When |
|--|--|
| 201 `{"status": "accepted", "event_id": N, "late": false}` | new event stored |
| 200 `{"status": "duplicate", "event_id": N, "late": false}` | same key and payload as a stored event; nothing stored |
| 409 | same key, different `model`, tokens or `occurred_at`; nothing stored |
| 403 | body `tenant_id` is not the tenant the edge admitted |
| 401 / 402 / 503 | the edge refused, nothing stored: no `X-Tenant-Id` header / no usable entitlement / entitlement check failed (see `docs/billing.md`) |
| 503 | the database went away after the edge check: nothing is confirmed stored, and resending the same key is safe |
| 422 | invalid body (naive or numeric timestamp, text too long or with a NUL, a count out of range). The body lists `type`, `loc` and `msg` only, never the input: echoing it made a `NaN` or a lone surrogate a 500 |

## How it stays correct

- **Resends:** one `INSERT ... ON CONFLICT (tenant_id, idempotency_key) DO NOTHING RETURNING id, late`.
  Two identical requests at once insert once; the loser gets no row back, reads the stored event and
  compares payloads. There is no check-then-insert.
- **Late events:** the same INSERT sets `late = true` when the event's UTC hour is already in
  `closed_hours` (`date_trunc('hour', occurred_at, 'UTC')`, so the session time zone cannot move
  the hour). A late event is answered 201 with `"late": true` (a resend says so too, as does the log
  line), is stored, and is never in a rollup, so it is **not billed**. Ingest first takes a shared lock
  for the event's hour (see Rollups). **How long a client has:** an event is on time until its hour is closed. With the
  hourly loop the close runs between 5 minutes (the grace) and about 65 minutes (grace plus one loop of 3600 s) after
  the hour ends, so a client that buffers through an outage and resends later than that gets `201` with `"late": true`
  and is never billed for that event.

**Tests:** `docker compose up -d db`, then `uv run pytest -q tests/test_ingest.py tests/test_attack_ingest.py` (`DATABASE_URL` if not port 5432). The second file is the hostile-input, race and outage tests.

**Does not do:** batch intake, or authentication beyond the edge dependency.

# Rollups (`app/rollup.py`)

`close_hour(conn, hour_start)` sums the non-late events of one UTC hour per (tenant, model) into
`usage_rollups_hourly` and inserts the `closed_hours` row, in one transaction. A second close of the
same hour changes nothing and returns `None`. `hour_start` must be a timezone-aware UTC hour boundary
(else `ValueError`).

`close_due_hours(conn, now, grace=5 min)` closes, oldest first, every unclosed hour that has events
and ended at least `grace` before `now`. An hour with no events is not closed.

**Never both, never neither.** Each event of a closed hour is in its closed rollup or flagged `late`. Ingest
takes the shared advisory lock `pg_advisory_xact_lock_shared(1, hour)` before its INSERT and holds it
to commit; `close_hour` takes the exclusive lock for the hour before it reads anything.
- An ingest already in flight finishes first, so the close counts its event (`late = false`).
- An ingest that arrives during the close waits; its INSERT then sees the hour in `closed_hours` and
  stores the event with `late = true`. A late event, a resend (200) or a conflict (409) never
  changes a closed rollup row.

**CLI:** `python -m app.rollup` closes the due hours once; `--every SECONDS` repeats. It prints
`closed hour=... rows=N events=N` per closed hour, or `nothing due`. A close runs with no statement timeout.
Compose runs it as the `rollup` service (`--every 3600`).

**Tests:** `uv run pytest -q tests/test_rollup.py`. The lock tests hold one side's lock by hand and
check the other side waits.

**Does not do:** reconciliation, pushing usage to Stripe, a query API over rollups, a queue.

**Known limit:** the rollup token columns are BIGINT. If one tenant's events for one model in one UTC hour
(taken from the client's `occurred_at`, so not necessarily the current hour) sum past 9.2e18 tokens, which is
9,223,373 events at the 10^12 per-event cap, closing that hour fails with `bigint out of range` and rolls back (no `closed_hours` row, no rollup rows).
The hour stays unclosed and is retried every run until the columns are widened to NUMERIC.

**A failed hour** is logged at ERROR as `rollup failed: hour=<UTC hour> error=...` (no tenant) and the later hours still close.
The one-shot run prints `rollup: closed N hours, failed M: <hours>` and exits 1; with `--every` the loop keeps sleeping and logs it every cycle.
Find it with `docker compose logs rollup | grep "rollup failed"`. Hours may close out of order; that never makes an event late.

**Timeouts:** every connection gives up connecting after 5 s; request-path connections cancel a statement after 15 s,
so a database that stops accepting connections, or answers slowly, is a 503 and `/healthz` keeps answering.
Known limit: a database that freezes after a connection was made (a paused container) is not bounded, because the
statement timeout is enforced by the server and the client has no timer of its own; the settings query right after
connecting is likewise not bounded (found by review, not fixed).
