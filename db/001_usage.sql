-- Usage events and hourly rollups. Owner: dev A (ingest + rollup).
-- Applied in filename order by app.db.apply_schema; every statement is repeatable.

-- Raw log. Append-only: a resent event hits the unique key and is counted once.
CREATE TABLE IF NOT EXISTS usage_events (
    id              BIGSERIAL PRIMARY KEY,
    tenant_id       TEXT        NOT NULL,
    idempotency_key TEXT        NOT NULL,
    model           TEXT        NOT NULL,
    tokens_in       BIGINT      NOT NULL CHECK (tokens_in >= 0),
    tokens_out      BIGINT      NOT NULL CHECK (tokens_out >= 0),
    occurred_at     TIMESTAMPTZ NOT NULL,
    received_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- true when the event's hour was already closed at ingest: kept in the log, excluded from the closed rollup
    late            BOOLEAN     NOT NULL DEFAULT FALSE,
    UNIQUE (tenant_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS usage_events_occurred_at ON usage_events (occurred_at);

CREATE OR REPLACE FUNCTION usage_events_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'usage_events is append-only';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS usage_events_no_change ON usage_events;
CREATE TRIGGER usage_events_no_change
    BEFORE UPDATE OR DELETE ON usage_events
    FOR EACH ROW EXECUTE FUNCTION usage_events_append_only();

-- An hour is closed once, for every tenant at the same time.
CREATE TABLE IF NOT EXISTS closed_hours (
    hour_start TIMESTAMPTZ PRIMARY KEY,
    closed_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS usage_rollups_hourly (
    tenant_id   TEXT        NOT NULL,
    model       TEXT        NOT NULL,
    hour_start  TIMESTAMPTZ NOT NULL,
    tokens_in   BIGINT      NOT NULL,
    tokens_out  BIGINT      NOT NULL,
    event_count BIGINT      NOT NULL,
    PRIMARY KEY (tenant_id, model, hour_start)
);
