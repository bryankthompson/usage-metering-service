-- Price table, entitlements and processed Stripe events. Owner: dev B (pricing + billing + edge).
-- Money is an integer count of micro-USD (1 USD = 1,000,000), never a float.

CREATE TABLE IF NOT EXISTS price_versions (
    model                 TEXT        NOT NULL,
    version               INT         NOT NULL,
    effective_from        TIMESTAMPTZ NOT NULL,
    input_micros_per_1k   BIGINT      NOT NULL CHECK (input_micros_per_1k >= 0),
    output_micros_per_1k  BIGINT      NOT NULL CHECK (output_micros_per_1k >= 0),
    PRIMARY KEY (model, version)
);

-- A new price is a new version: a stored version is never changed or removed, so a past charge can be rebuilt.
CREATE OR REPLACE FUNCTION price_versions_immutable() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'price_versions is append-only: add a new version instead';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS price_versions_no_change ON price_versions;
CREATE TRIGGER price_versions_no_change
    BEFORE UPDATE OR DELETE ON price_versions
    FOR EACH ROW EXECUTE FUNCTION price_versions_immutable();

CREATE TABLE IF NOT EXISTS entitlements (
    tenant_id              TEXT PRIMARY KEY,
    stripe_customer_id     TEXT,
    stripe_subscription_id TEXT,
    plan                   TEXT,
    status                 TEXT        NOT NULL,   -- active | past_due | canceled
    payment_failed_at      TIMESTAMPTZ,
    grace_until            TIMESTAMPTZ,
    last_event_at          TIMESTAMPTZ,            -- `created` of the newest Stripe event applied: older events never overwrite
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One row per Stripe event handled: a redelivered webhook changes nothing the second time.
CREATE TABLE IF NOT EXISTS stripe_events (
    event_id    TEXT PRIMARY KEY,
    type        TEXT        NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
