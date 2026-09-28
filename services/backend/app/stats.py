"""Tenant statistics over a date range, three ways.

approximate (sqs mode, default): merge the daily sketches. Cost depends on the
    number of days, not the number of events. A year of data is 365 × 3 small
    rows, and unique users across days come out right because HLL merge is a
    true union.
exact (sqs mode, ?exact=true): scatter-gather every event from DynamoDB and
    count precisely. Cost grows with data volume, so the range is capped. It
    exists to verify the sketches.
exact (postgres mode): plain SQL aggregates over the events table.
"""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from datetime import UTC, date, datetime, time, timedelta
from uuid import UUID

import asyncpg
from pydantic import BaseModel

from .aggregates import KINDS, SketchConfig, load
from .store import DynamoEventStore


class Ranked(BaseModel):
    key: str
    count: int


class Stats(BaseModel):
    start: date
    end: date
    approximate: bool
    events: int
    unique_users: int
    top_events: list[Ranked]
    top_pages: list[Ranked]


def _ranked(pairs, limit: int) -> list[Ranked]:
    return [Ranked(key=k, count=c) for k, c in sorted(pairs, key=lambda p: (-p[1], p[0]))[:limit]]


async def approximate_stats(
    pool: asyncpg.Pool, tenant_id: UUID, start: date, end: date, cfg: SketchConfig, limit: int
) -> Stats:
    rows = await pool.fetch(
        "SELECT kind, data FROM sketches WHERE tenant_id = $1 AND day BETWEEN $2 AND $3 AND data IS NOT NULL",
        tenant_id,
        start,
        end,
    )
    merged = {kind: cfg.new(kind) for kind in KINDS}
    for r in rows:
        merged[r["kind"]].merge(load(r["kind"], r["data"]))
    return Stats(
        start=start,
        end=end,
        approximate=True,
        events=merged["events"].total,
        unique_users=round(merged["users"].estimate()),
        top_events=_ranked(merged["events"].top(), limit),
        top_pages=_ranked(merged["pages"].top(), limit),
    )


async def exact_stats_dynamo(
    store: DynamoEventStore, tenant_id: UUID, start: date, end: date, limit: int
) -> Stats:
    days = [start + timedelta(d) for d in range((end - start).days + 1)]
    per_day = await asyncio.gather(*(store.query_day(str(tenant_id), d) for d in days))
    users: set[str] = set()
    names: Counter = Counter()
    pages: Counter = Counter()
    for items in per_day:
        for it in items:
            users.add(it["distinct_id"]["S"])
            names[it["name"]["S"]] += 1
            path = json.loads(it["properties"]["S"])["properties"].get("path")
            if it["name"]["S"] == "page_view" and isinstance(path, str):
                pages[path] += 1
    return Stats(
        start=start,
        end=end,
        approximate=False,
        events=sum(names.values()),
        unique_users=len(users),
        top_events=_ranked(names.items(), limit),
        top_pages=_ranked(pages.items(), limit),
    )


# Half-open UTC range [start 00:00, end+1 00:00) so the (tenant_id, …, occurred_at) indexes apply.
TOTALS_SQL = """
SELECT count(*) AS events, count(DISTINCT distinct_id) AS users
FROM events WHERE tenant_id = $1 AND occurred_at >= $2 AND occurred_at < $3
"""
TOP_EVENTS_SQL = """
SELECT name AS key, count(*) AS count
FROM events WHERE tenant_id = $1 AND occurred_at >= $2 AND occurred_at < $3
GROUP BY name ORDER BY count DESC, key LIMIT $4
"""
TOP_PAGES_SQL = """
SELECT properties->>'path' AS key, count(*) AS count
FROM events
WHERE tenant_id = $1 AND name = 'page_view' AND occurred_at >= $2 AND occurred_at < $3
  AND jsonb_typeof(properties->'path') = 'string'
GROUP BY 1 ORDER BY count DESC, key LIMIT $4
"""


async def exact_stats_postgres(
    pool: asyncpg.Pool, tenant_id: UUID, start: date, end: date, limit: int
) -> Stats:
    lo = datetime.combine(start, time.min, UTC)
    hi = datetime.combine(end + timedelta(days=1), time.min, UTC)
    totals = await pool.fetchrow(TOTALS_SQL, tenant_id, lo, hi)
    top_events = await pool.fetch(TOP_EVENTS_SQL, tenant_id, lo, hi, limit)
    top_pages = await pool.fetch(TOP_PAGES_SQL, tenant_id, lo, hi, limit)
    return Stats(
        start=start,
        end=end,
        approximate=False,
        events=totals["events"],
        unique_users=totals["users"],
        top_events=[Ranked(**dict(r)) for r in top_events],
        top_pages=[Ranked(**dict(r)) for r in top_pages],
    )
