-- 003_sketches.sql — per-tenant, per-day probabilistic sketches.
--
-- Workers update these in the same transaction as usage_ledger, so a sketch
-- absorbs each queue message exactly once (see ADR 0004). Daily sketches merge
-- losslessly into any date range: unique users over a week is the union of seven
-- HyperLogLogs, not the sum of seven counts.
CREATE TABLE sketches (
    tenant_id  UUID        NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    day        DATE        NOT NULL,   -- UTC day of occurred_at
    kind       TEXT        NOT NULL CHECK (kind IN ('users', 'events', 'pages')),
    data       BYTEA,                  -- NULL = placeholder row created to take the lock
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, day, kind)
);
