-- 002_usage_ledger.sql — exactly-once billing for the async pipeline.
--
-- SQS delivers at-least-once, and workers can crash mid-message. The worker
-- records one ledger row per queue message (batch_id) in the same statement that
-- bumps usage_daily. A redelivered message hits ON CONFLICT DO NOTHING, so its
-- usage is applied exactly once no matter how many times it is processed.
CREATE TABLE usage_ledger (
    batch_id     UUID        PRIMARY KEY,
    tenant_id    UUID        NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    day          DATE        NOT NULL,
    event_count  INTEGER     NOT NULL CHECK (event_count >= 0),
    processed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Rows only need to outlive SQS's maximum retention (14 days); this index makes
-- the periodic prune (DELETE ... WHERE processed_at < now() - '15 days') cheap.
CREATE INDEX usage_ledger_processed_at_idx ON usage_ledger (processed_at);
