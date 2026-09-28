# 4. Sketch updates share the billing transaction

**Status:** Accepted

## Context
Phase 3 adds per-tenant, per-day sketches: HyperLogLog for unique users, and TopK (Count-Min plus a heap) for top events and pages. They power `/v1/stats` without scanning raw events.

Sketches inherit the delivery problem from ADR 2. HyperLogLog is idempotent (re-adding an item changes nothing), but **Count-Min is additive**. Absorbing a redelivered message twice would permanently inflate counts, and unlike billing, nobody would notice.

## Options considered
1. **In-memory sketches in each worker, flushed periodically.** Fast, but a crash loses everything since the last flush. Recovering would need replay, and SQS has no offsets to replay from.
2. **Separate sketch updates after billing.** A crash between the two commits leaves billing applied and sketches not. The ledger then marks the batch done, so the sketches never catch up.
3. **Update sketches in the same Postgres transaction as the ledger insert.** Chosen.

## Decision
Each poll of up to 10 messages writes events to DynamoDB in parallel, then runs **one** transaction that:
1. inserts a `usage_ledger` row per batch (`ON CONFLICT DO NOTHING RETURNING batch_id`);
2. for *only the newly inserted* batches, adds usage and feeds their owned events into the tenant-day sketches;
3. commits. Only then are the SQS messages deleted.

A message therefore updates ledger, usage, and sketches together, or not at all.

**Locking.** Tenant affinity (ADR 3) means one worker normally owns a tenant's rows, but during a shard rebalance two workers can overlap. So we:
1. insert placeholder rows (`ON CONFLICT DO NOTHING`) so every row to be touched exists;
2. `SELECT … ORDER BY key FOR UPDATE`, which locks in a global order so there are no deadlocks;
3. read, merge, and write under the lock.

Both steps were verified by **mutation testing**: `test_concurrent_commits_to_one_sketch_lose_nothing` races 20 transactions on one tenant-day.

| Variant | Events recorded (of 200) |
|---|---|
| As designed | 200 |
| Without `FOR UPDATE` | 20–30 |
| With `FOR UPDATE` but no placeholder rows (nothing to lock yet) | 110 |

The first version of that test passed even with locks removed: the `usage_daily` upsert was serializing transactions by accident, because every test batch had the same billing day. Real traffic breaks that coincidence (billing day ≠ event day for late or backfilled events), so the test now uses distinct billing days.

## Consequences
* Stats are exactly-once, like billing: redelivery and mid-message crashes are covered by tests.
* Sketches are durable and survive worker restarts with no warm-up.
* Cost: each poll reads and writes the touched tenant-days' sketches (≈16 KiB HLL + 2 × 106 KiB TopK at defaults). Batching 10 messages per transaction amortizes it. If it ever dominates, the next steps are a larger poll window, or delta sketches merged lazily at query time.
* Daily sketches merge into any range, so `/v1/stats` costs O(days), not O(events). Measured locally: 9 ms from sketches vs 1.5 s for the exact DynamoDB scan on 5k events. The gap grows with volume.
