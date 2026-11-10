# Usage intake: `POST /v1/usage`

Stores one usage event in the append-only `usage_events` table (`db/001_usage.sql`), behind the edge
check `require_entitlement` and one database connection per request.

Body, all fields required: `tenant_id`, `idempotency_key` (1 to 200 chars), `model` (non-empty),
`tokens_in` and `tokens_out` (integers >= 0), `occurred_at` (ISO 8601 with a time zone).

| Status | When |
|--|--|
| 201 `{"status": "accepted", "event_id": N}` | new event stored |
| 200 `{"status": "duplicate", "event_id": N}` | same `(tenant_id, idempotency_key)`, same payload; nothing stored |
| 409 | same key, different `model`, tokens or `occurred_at`; nothing stored |
| 403 | body `tenant_id` is not the tenant the edge admitted |
| 422 | invalid body, including a naive timestamp |

## How it stays correct

- **Resends:** one `INSERT ... ON CONFLICT (tenant_id, idempotency_key) DO NOTHING RETURNING id`.
  Two identical requests at the same instant insert once; the loser gets no row back, reads the
  stored event and compares payloads. There is no check-then-insert.
- **Late events:** the same INSERT sets `late = true` when the event's UTC hour is already in
  `closed_hours` (`date_trunc('hour', occurred_at, 'UTC')`, so the session time zone cannot move
  the hour). A late event is stored and answered 201.

**Tests:** `docker compose up -d db`, then `uv run pytest -q tests/test_ingest.py` (set `DATABASE_URL`
if the database is not on port 5432).

**Does not do:** rollups (next slice), batch intake, a token-count limit beyond Postgres `BIGINT` (larger is a 500).
