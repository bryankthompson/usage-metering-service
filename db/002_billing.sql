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

CREATE TABLE IF NOT EXISTS entitlements (
    tenant_id              TEXT PRIMARY KEY,
    stripe_customer_id     TEXT,
    stripe_subscription_id TEXT,
    plan                   TEXT,
    status                 TEXT        NOT NULL,   -- active | past_due | canceled
    payment_failed_at      TIMESTAMPTZ,
    grace_until            TIMESTAMPTZ,
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One row per Stripe event handled: a redelivered webhook changes nothing the second time.
CREATE TABLE IF NOT EXISTS stripe_events (
    event_id    TEXT PRIMARY KEY,
    type        TEXT        NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
