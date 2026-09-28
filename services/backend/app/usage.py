"""Exactly-once usage accounting for the async pipeline.

One statement: insert the ledger row for this batch; only if that insert
happened (first time we've seen batch_id) add its count to usage_daily.
Redelivered messages insert nothing, so they add nothing.
"""

from datetime import date
from uuid import UUID

import asyncpg

RECORD_SQL = """
WITH led AS (
    INSERT INTO usage_ledger (batch_id, tenant_id, day, event_count)
    VALUES ($1, $2, $3, $4)
    ON CONFLICT (batch_id) DO NOTHING
    RETURNING tenant_id, day, event_count
)
INSERT INTO usage_daily (tenant_id, day, event_count)
SELECT tenant_id, day, event_count FROM led WHERE event_count > 0
ON CONFLICT (tenant_id, day)
DO UPDATE SET event_count = usage_daily.event_count + EXCLUDED.event_count
"""


async def record_usage(pool: asyncpg.Pool, batch_id: UUID, tenant_id: UUID, day: date, count: int) -> None:
    await pool.execute(RECORD_SQL, batch_id, tenant_id, day, count)
