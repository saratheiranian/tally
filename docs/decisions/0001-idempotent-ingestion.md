# 1. Idempotent ingestion via client-generated event IDs

**Status:** Accepted

## Context
Clients send events over unreliable networks and will retry on timeouts. If a retry arrives after the first attempt actually succeeded, a naive insert double-counts, corrupting analytics *and* billing.

## Options considered
1. **Server-generated IDs + best-effort.** Simple, but double-counts on every retry.
2. **Idempotency-Key header per request, cached in Redis.** Works per request, but a partially-applied batch is hard to reason about, and the cache TTL bounds safety.
3. **Client-generated UUID per event, enforced by a DB primary key.** Dedupe is permanent and per event, and it is enforced by the database rather than by application logic.

## Decision
Option 3. `(tenant_id, event_id)` is the primary key; inserts use `ON CONFLICT DO NOTHING`. Scoping by tenant means one tenant cannot block another's IDs.

## Consequences
- Retries are always safe; the SDK/client just resends the same batch.
- Billing counts only newly inserted rows (same statement), so it cannot drift.
- The PK index grows with the table. In Phase 2 the equivalent is a DynamoDB conditional put (`attribute_not_exists`) on the same key.
