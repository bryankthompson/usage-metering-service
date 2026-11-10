# Usage intake: `POST /v1/usage`

Stores one usage event in the append-only `usage_events` table (`db/001_usage.sql`) behind the edge check
`require_entitlement`, which reads the entitlement on its own connection (the handler uses a second one).

Body (all required): `tenant_id`, `idempotency_key` (1 to 200 chars), `model`, `tokens_in` and `tokens_out` (integers 0 to 10^12), `occurred_at` (ISO 8601 with a time zone).

| Status | When |
|--|--|
| 201 `{"status": "accepted", "event_id": N, "late": false}` | new event stored |
| 200 `{"status": "duplicate", "event_id": N, "late": false}` | same key and payload as a stored event; nothing stored |
| 409 | same key, different `model`, tokens or `occurred_at`; nothing stored |
| 403 | body `tenant_id` is not the tenant the edge admitted |
| 401 / 402 / 503 | the edge refused, nothing stored: no `X-Tenant-Id` header / no usable entitlement / entitlement check failed (see `docs/billing.md`) |
| 422 | invalid body, including a naive timestamp |

## How it stays correct

- **Resends:** one `INSERT ... ON CONFLICT (tenant_id, idempotency_key) DO NOTHING RETURNING id, late`.
  Two identical requests at once insert once; the loser gets no row back, reads the stored event and
  compares payloads. There is no check-then-insert.
- **Late events:** the same INSERT sets `late = true` when the event's UTC hour is already in
  `closed_hours` (`date_trunc('hour', occurred_at, 'UTC')`, so the session time zone cannot move
  the hour). A late event is answered 201 with `"late": true` (a resend says so too, as does the log
  line), is stored, and is never in a rollup, so it is **not billed**. Ingest first takes a shared lock
  for the event's hour (see Rollups).

**Tests:** `docker compose up -d db`, then `uv run pytest -q tests/test_ingest.py` (`DATABASE_URL` if not port 5432).

**Does not do:** batch intake, or authentication beyond the edge dependency.

# Rollups (`app/rollup.py`)

`close_hour(conn, hour_start)` sums the non-late events of one UTC hour per (tenant, model) into
`usage_rollups_hourly` and inserts the `closed_hours` row, in one transaction. A second close of the
same hour changes nothing and returns `None`. `hour_start` must be a timezone-aware UTC hour boundary
(else `ValueError`).

`close_due_hours(conn, now, grace=5 min)` closes, oldest first, every unclosed hour that has events
and ended at least `grace` before `now`. An hour with no events is not closed.

**Never both, never neither.** Each event is in its hour's closed rollup or flagged `late`. Ingest
takes the shared advisory lock `pg_advisory_xact_lock_shared(1, hour)` before its INSERT and holds it
to commit; `close_hour` takes the exclusive lock for the hour before it reads anything.
- An ingest already in flight finishes first, so the close counts its event (`late = false`).
- An ingest that arrives during the close waits; its INSERT then sees the hour in `closed_hours` and
  stores the event with `late = true`. A late event, a resend (200) or a conflict (409) never
  changes a closed rollup row.

**CLI:** `python -m app.rollup` closes the due hours once; `--every SECONDS` repeats. It prints
`closed hour=... rows=N events=N` per closed hour, or `nothing due`, and exits non-zero on an error.
Compose runs it as the `rollup` service (`--every 3600`).

**Tests:** `uv run pytest -q tests/test_rollup.py`. The lock tests hold one side's lock by hand and
check the other side waits.

**Does not do:** reconciliation, pushing usage to Stripe, a query API over rollups, a queue.

**Known limit:** the rollup token columns are BIGINT. If one tenant's events for one model in one UTC hour
(taken from the client's `occurred_at`, so not necessarily the current hour) sum past 9.2e18 tokens, which is
9,223,373 events at the 10^12 per-event cap, closing that hour fails with `bigint out of range`, rolls back, and
`python -m app.rollup` exits non-zero. Compose restarts the service, so it fails again, loudly, and no later hour
closes until the columns are widened to NUMERIC.
