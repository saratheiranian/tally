# 2. Exactly-once billing over at-least-once delivery

**Status:** Accepted

## Context
Phase 2 puts SQS between ingest and storage. SQS standard queues deliver **at least once**, and a worker can die at any point in a message. A message has two effects that must both land exactly once: storing its events, and adding them to the tenant's bill.

The trap: the obvious approach is to bill only events that were *newly inserted*. If a worker writes the events, then crashes before billing, the redelivered message finds every event already present, inserts nothing, and bills **zero**, silently and forever.

## Options considered
1. **Distributed transaction across DynamoDB and Postgres.** Not available; two-phase commit across these systems isn't a real option.
2. **SQS FIFO exactly-once processing.** Dedupe only covers a 5-minute window and only on the send side. It doesn't protect against a consumer crashing mid-message. Throughput is also capped per message group.
3. **Make every step idempotent, keyed on a stable batch ID.** Chosen.

## Decision
The ingest tier stamps each queue message with a `batch_id` that never changes across redeliveries.

* **Event writes** use a DynamoDB conditional put: `attribute_not_exists(pk) OR batch_id = :this_batch`. A redelivery of the same message re-claims its own events, so they count again. A *different* message containing the same event (a client retry) is rejected as a true duplicate.
* **Billing** inserts one row per `batch_id` into `usage_ledger` and adds to `usage_daily` in the same statement, only if that ledger insert happened. A second attempt adds nothing.
* The message is deleted from SQS only after both steps succeed.

A crash between any two steps is safe, because re-running converges on the same state. `test_crash_after_dynamo_before_billing_recovers_exactly` simulates the exact failure described above.

## Consequences
* Correct billing under redelivery, worker crashes, and client retries, all covered by tests.
* One DynamoDB write per event (no BatchWriteItem, which can't carry conditions). This costs the same WCUs; we recover throughput with bounded parallelism (`worker_concurrency`).
* `usage_ledger` grows by one row per message. It only needs to outlive SQS's 14-day maximum retention, so it can be pruned; there's an index on `processed_at` for that.
* Event identity includes `occurred_at` (it's in the key), so clients must retry with an identical payload.
