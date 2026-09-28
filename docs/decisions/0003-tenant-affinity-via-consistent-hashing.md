# 3. Tenant-to-shard routing with a consistent-hash ring

**Status:** Accepted

## Context
Phase 3 keeps per-tenant probabilistic sketches (HyperLogLog, Count-Min) in worker memory. That only works if **all of a tenant's events reach the same worker**. A single shared SQS queue hands any message to any consumer.

## Options considered
1. **One shared queue, sketches in Redis.** Every event becomes a network round trip to Redis; it adds latency and a hot dependency.
2. **SQS FIFO with `MessageGroupId = tenant_id`.** Gives per-tenant ordering, but not consumer affinity: groups still move between consumers. It also caps throughput per group.
3. **N shard queues; route `tenant → shard` with `hash(tenant) % N`.** Simple, but changing N remaps ~all tenants, which throws away every worker's in-memory state at once.
4. **N shard queues; route with a consistent-hash ring.** Chosen.

## Decision
The ingest tier builds a hash ring over the shard queue names, with 160 virtual nodes each, and sends each tenant's events to `ring.get(tenant_id)`. Each worker consumes a configured subset of shards (`TALLY_WORKER_SHARDS`).

The hash is MD5, not Python's `hash()`, which is salted per process: every API node must compute the same answer. A test runs the ring under different `PYTHONHASHSEED`s to prove it.

## Consequences
* Adding a 5th shard moves ~20% of tenants, and only onto the new shard (tested). With mod-N it would be ~80%.
* Virtual nodes keep shard load within ±20% of even (tested over 20k tenants).
* A very large tenant still maps to one shard. If that becomes a problem, the next step is splitting large tenants into sub-keys (`tenant#0..k`) and merging their sketches, which HLL and Count-Min support natively.
