"""Where accepted events go.

PostgresSink writes synchronously (simple single-node mode). SqsSink enqueues for
the worker fleet. The API depends only on the EventSink protocol, so switching is
config-only (TALLY_SINK).
"""

import asyncio
import json
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID, uuid4

import asyncpg

from .models import Event, IngestResult, QueueMessage
from .queueing import chunk_events, pack_bodies


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


class SinkUnavailable(RuntimeError):
    """The queue rejected part of a request. The client should retry the whole
    batch; that is safe because workers dedupe by event_id."""


class SqsSink:
    """Enqueues events on the tenant's shard queue and returns immediately.

    Because the ring always maps a tenant to the same queue, one worker sees all
    of a tenant's events, which Phase 3's per-tenant sketches rely on.
    """

    def __init__(self, sqs, queue_urls: dict[str, str], ring) -> None:
        self._sqs = sqs
        self._urls = queue_urls
        self._ring = ring

    def queue_for(self, tenant_id: UUID) -> str:
        return self._ring.get(str(tenant_id))

    async def write(self, tenant_id: UUID, events: list[Event]) -> IngestResult:
        url = self._urls[self.queue_for(tenant_id)]
        received_at = datetime.now(UTC)
        bodies = [
            QueueMessage(
                batch_id=uuid4(), tenant_id=tenant_id, received_at=received_at, events=chunk
            ).model_dump_json()
            for chunk in chunk_events(events)
        ]
        for group in pack_bodies(bodies):
            entries = [{"Id": str(i), "MessageBody": body} for i, body in enumerate(group)]
            resp = await asyncio.to_thread(self._sqs.send_message_batch, QueueUrl=url, Entries=entries)
            if resp.get("Failed"):
                raise SinkUnavailable(f"{len(resp['Failed'])} message(s) rejected by SQS")
        return IngestResult(accepted=len(events), duplicates=None)
