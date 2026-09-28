"""Apply a poll's worth of processed messages to Postgres in ONE transaction:
billing ledger, daily usage, and per-tenant sketches.

Why one transaction: sketches must absorb each message exactly once, like
billing does. Count-Min is additive, so double-applying a redelivered message
would inflate counts. Tying sketch updates to the ledger insert means a message
either updates ledger, usage, and sketches together, or none of them.

Concurrency: a tenant's events arrive on one shard (consistent hashing), so one
worker normally owns its sketch rows and locks are uncontended. During a
rebalance two workers could touch the same rows, so we still lock correctly:
  1. insert placeholder rows (ON CONFLICT DO NOTHING) so every row exists;
  2. SELECT ... ORDER BY key FOR UPDATE, which locks in a global order (no deadlocks);
  3. read-modify-write under the lock.
Without step 1, two workers could both find "no row", both build from empty,
and the second write would silently discard the first (a lost update).
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, date
from uuid import UUID

import asyncpg
from tally_sketches import HyperLogLog, TopK

from .models import Event

KINDS = ("events", "pages", "users")


@dataclass(frozen=True)
class SketchConfig:
    hll_precision: int = 14
    topk_k: int = 20
    topk_epsilon: float = 0.001

    def new(self, kind: str):
        return HyperLogLog(self.hll_precision) if kind == "users" else TopK(self.topk_k, self.topk_epsilon)


def load(kind: str, data: bytes):
    return HyperLogLog.from_bytes(data) if kind == "users" else TopK.from_bytes(data)


def feed(sketches: dict, events: list[Event]) -> None:
    for e in events:
        sketches["users"].add(e.distinct_id)
        sketches["events"].add(e.name)
        path = e.properties.get("path")
        if e.name == "page_view" and isinstance(path, str):
            sketches["pages"].add(path)


@dataclass(frozen=True)
class ProcessedBatch:
    batch_id: UUID
    tenant_id: UUID
    received_day: date  # billing day (server clock)
    owned: list[Event]  # events this batch owns after DynamoDB dedupe


LEDGER_SQL = """
INSERT INTO usage_ledger (batch_id, tenant_id, day, event_count)
SELECT * FROM unnest($1::uuid[], $2::uuid[], $3::date[], $4::int[])
ON CONFLICT (batch_id) DO NOTHING
RETURNING batch_id
"""

USAGE_SQL = """
INSERT INTO usage_daily (tenant_id, day, event_count)
SELECT * FROM unnest($1::uuid[], $2::date[], $3::bigint[])
ON CONFLICT (tenant_id, day)
DO UPDATE SET event_count = usage_daily.event_count + EXCLUDED.event_count
"""

PLACEHOLDER_SQL = """
INSERT INTO sketches (tenant_id, day, kind)
SELECT * FROM unnest($1::uuid[], $2::date[], $3::text[])
ON CONFLICT DO NOTHING
"""

LOCK_SQL = """
SELECT s.tenant_id, s.day, s.kind, s.data
FROM sketches s
JOIN unnest($1::uuid[], $2::date[], $3::text[]) AS k(t, d, kind)
  ON s.tenant_id = k.t AND s.day = k.d AND s.kind = k.kind
ORDER BY s.tenant_id, s.day, s.kind
FOR UPDATE OF s
"""

WRITE_SQL = """
UPDATE sketches s SET data = v.data, updated_at = now()
FROM unnest($1::uuid[], $2::date[], $3::text[], $4::bytea[]) AS v(t, d, kind, data)
WHERE s.tenant_id = v.t AND s.day = v.d AND s.kind = v.kind
"""


async def commit_batches(pool: asyncpg.Pool, batches: list[ProcessedBatch], cfg: SketchConfig) -> set[UUID]:
    """Returns the batch_ids applied for the first time (the rest were redeliveries)."""
    # SQS can hand the same message to one poll twice; collapse before the ledger.
    batches = list({b.batch_id: b for b in batches}.values())
    if not batches:
        return set()

    async with pool.acquire() as conn, conn.transaction():
        rows = await conn.fetch(
            LEDGER_SQL,
            [b.batch_id for b in batches],
            [b.tenant_id for b in batches],
            [b.received_day for b in batches],
            [len(b.owned) for b in batches],
        )
        new_ids = {r["batch_id"] for r in rows}
        fresh = [b for b in batches if b.batch_id in new_ids]

        usage: Counter = Counter()
        groups: dict[tuple[UUID, date], list[Event]] = defaultdict(list)
        for b in fresh:
            usage[(b.tenant_id, b.received_day)] += len(b.owned)
            for e in b.owned:
                groups[(b.tenant_id, e.occurred_at.astimezone(UTC).date())].append(e)

        usage = {k: v for k, v in usage.items() if v}
        if usage:
            keys = sorted(usage)
            await conn.execute(
                USAGE_SQL, [t for t, _ in keys], [d for _, d in keys], [usage[k] for k in keys]
            )

        if groups:
            await _update_sketches(conn, groups, cfg)
    return new_ids


async def _update_sketches(conn, groups: dict[tuple[UUID, date], list[Event]], cfg: SketchConfig) -> None:
    keys = sorted((t, d, kind) for (t, d) in groups for kind in KINDS)
    cols = ([k[0] for k in keys], [k[1] for k in keys], [k[2] for k in keys])
    await conn.execute(PLACEHOLDER_SQL, *cols)
    current = {(r["tenant_id"], r["day"], r["kind"]): r["data"] for r in await conn.fetch(LOCK_SQL, *cols)}

    out = []
    for (tenant, day), events in sorted(groups.items()):
        sketches = {}
        for kind in KINDS:
            data = current.get((tenant, day, kind))
            sketches[kind] = load(kind, data) if data else cfg.new(kind)
        feed(sketches, events)
        out.extend((tenant, day, kind, sketches[kind].to_bytes()) for kind in KINDS)

    await conn.execute(WRITE_SQL, *(list(col) for col in zip(*out, strict=True)))
