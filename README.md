# usage-metering-service

A small prototype of a usage metering and billing service: idempotent usage-event intake, hourly
rollups per tenant and model, a versioned price table, Stripe test-mode webhooks feeding an
entitlement record, and a fail-closed check at the API edge.

**Status: scaffold.** This README describes only what the code does today; sections are added as each
piece merges.

## Run it

```bash
docker compose up -d db
DATABASE_URL=postgresql://metering:metering@localhost:5432/metering uv run pytest -q
```

## Layout

| Path | What |
|--|--|
| `db/*.sql` | schema, applied in filename order; every file is repeatable |
| `app/ingest.py` | usage-event intake (dev A) |
| `app/rollup.py` | hourly rollups (dev A) |
| `app/pricing.py`, `app/webhooks.py`, `app/edge.py` | price table, Stripe webhooks, edge check (dev B) |
| `docs/build-log.md` | how the work was split, reviewed and merged |
