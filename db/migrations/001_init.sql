-- 001_init.sql — core relational schema for Tally.
-- Postgres owns everything relational: tenants, people, credentials, billing usage.
-- Raw events live here in Phase 1 and move to DynamoDB in Phase 2.

CREATE EXTENSION IF NOT EXISTS pgcrypto;  -- gen_random_uuid()

-- Tenants: one row per customer organisation. Rate-limit settings live here so
-- they can change per plan without redeploying the ingest tier.
CREATE TABLE tenants (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name               TEXT        NOT NULL,
    plan               TEXT        NOT NULL DEFAULT 'free'
                                   CHECK (plan IN ('free', 'pro', 'enterprise')),
    rate_limit_per_sec INTEGER     NOT NULL DEFAULT 100 CHECK (rate_limit_per_sec > 0),
    rate_limit_burst   INTEGER     NOT NULL DEFAULT 500 CHECK (rate_limit_burst > 0),
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Dashboard users (humans), scoped to a tenant.
CREATE TABLE users (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id  UUID        NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    email      TEXT        NOT NULL,
    role       TEXT        NOT NULL DEFAULT 'viewer'
                           CHECK (role IN ('owner', 'admin', 'viewer')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, email)
);

-- API keys (machines). Only a SHA-256 hash is stored; the plaintext key is shown
-- once at creation. key_prefix lets humans identify a key without revealing it.
CREATE TABLE api_keys (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID        NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    name        TEXT        NOT NULL,
    key_prefix  TEXT        NOT NULL,
    key_hash    BYTEA       NOT NULL UNIQUE,   -- unique index doubles as lookup index
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at  TIMESTAMPTZ
);
CREATE INDEX api_keys_tenant_idx ON api_keys (tenant_id);

-- Raw events (Phase 1 storage).
-- The composite primary key (tenant_id, event_id) is the idempotency guarantee:
-- clients generate event_id, so a retried request can never double-count.
CREATE TABLE events (
    tenant_id   UUID        NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    event_id    UUID        NOT NULL,
    name        TEXT        NOT NULL,
    distinct_id TEXT        NOT NULL,                 -- the end-user being tracked
    properties  JSONB       NOT NULL DEFAULT '{}'::jsonb,
    occurred_at TIMESTAMPTZ NOT NULL,                 -- client clock
    received_at TIMESTAMPTZ NOT NULL DEFAULT now(),   -- server clock
    PRIMARY KEY (tenant_id, event_id)
);
-- Serves "count of event X for tenant T over range R", the most common query.
CREATE INDEX events_tenant_name_time_idx ON events (tenant_id, name, occurred_at);
-- Serves per-user timelines and funnel queries.
CREATE INDEX events_tenant_user_time_idx ON events (tenant_id, distinct_id, occurred_at);

-- Daily usage for billing, upserted in the same transaction as the event insert
-- and counting only newly inserted events (duplicates are free).
CREATE TABLE usage_daily (
    tenant_id   UUID   NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    day         DATE   NOT NULL,
    event_count BIGINT NOT NULL DEFAULT 0 CHECK (event_count >= 0),
    PRIMARY KEY (tenant_id, day)
);
