"""Where accepted events go.

Phase 1 writes straight to Postgres. Phase 2 adds an SqsSink that enqueues for the
worker fleet; the API code will not change because it depends only on EventSink.
"""

import json
from typing import Protocol
from uuid import UUID

import asyncpg

from .models import Event, IngestResult


class EventSink(Protocol):
    async def write(self, tenant_id: UUID, events: list[Event]) -> IngestResult: ...


# One round trip, one atomic statement:
#  1. unnest() turns parallel arrays into rows, giving a set-based bulk insert.
#  2. ON CONFLICT DO NOTHING drops retries/duplicates via the (tenant_id, event_id) PK.
#  3. A second data-modifying CTE bumps billing usage by the *actually inserted* count.
# Billing uses the server-side day (now()), so clients can't shift usage by backdating.
INSERT_SQL = """
WITH ins AS (
    INSERT INTO events (tenant_id, event_id, name, distinct_id, properties, occurred_at)
    SELECT $1, e, n, d, p::jsonb, o
    FROM unnest($2::uuid[], $3::text[], $4::text[], $5::text[], $6::timestamptz[])
         AS t(e, n, d, p, o)
    ON CONFLICT (tenant_id, event_id) DO NOTHING
    RETURNING 1
), usage AS (
    INSERT INTO usage_daily (tenant_id, day, event_count)
    SELECT $1, (now() AT TIME ZONE 'UTC')::date, count(*)
    FROM ins
    HAVING count(*) > 0
    ON CONFLICT (tenant_id, day)
    DO UPDATE SET event_count = usage_daily.event_count + EXCLUDED.event_count
)
SELECT count(*) FROM ins
"""


class PostgresSink:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def write(self, tenant_id: UUID, events: list[Event]) -> IngestResult:
        inserted = await self._pool.fetchval(
            INSERT_SQL,
            tenant_id,
            [e.event_id for e in events],
            [e.name for e in events],
            [e.distinct_id for e in events],
            [json.dumps(e.properties) for e in events],
            [e.occurred_at for e in events],
        )
        return IngestResult(accepted=inserted, duplicates=len(events) - inserted)
