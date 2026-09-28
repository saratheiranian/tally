"""DynamoDB event store.

Key design
----------
    pk = "<tenant_id>#<yyyy-mm-dd>#<shard>"    shard = hash(event_id) % write_shards
    sk = "<occurred_at ISO-8601>#<event_id>"

Access pattern: "a tenant's events for a day, in time order". A naive
pk = tenant#day puts a big tenant's whole day in one partition, which caps out
at ~1,000 writes/s. Suffixing a deterministic shard spreads writes across
`write_shards` partitions. Reads scatter to every shard and k-way merge the
already-sorted results (heapq.merge, O(n log k)).

Exactly-once in effect
----------------------
Each put is conditional:  attribute_not_exists(pk) OR batch_id = :this_batch
  * new event                         -> written, counted
  * retry of *this same* message      -> overwritten with identical data, counted
    (the previous attempt crashed before billing, so it must count now)
  * same event from a *different* msg -> rejected: a true duplicate, not counted
The count feeds the usage ledger, which is idempotent per batch_id, so billing
is exact under redelivery, worker crashes, and client retries.

Invariant: event identity includes occurred_at, so clients must retry with the
identical payload (which retrying the same request does).
"""

import asyncio
import hashlib
import heapq
from datetime import UTC, date

from botocore.exceptions import ClientError

from .models import Event, QueueMessage


def shard_of(event_id: str, shards: int) -> int:
    return int.from_bytes(hashlib.md5(event_id.encode()).digest()[:4], "big") % shards


def item_keys(tenant_id: str, event: Event, shards: int) -> tuple[str, str]:
    eid = str(event.event_id)
    # Normalise to UTC: ISO strings only sort chronologically if they share an offset.
    ts = event.occurred_at.astimezone(UTC)
    pk = f"{tenant_id}#{ts.date().isoformat()}#{shard_of(eid, shards)}"
    sk = f"{ts.isoformat()}#{eid}"
    return pk, sk


class DynamoEventStore:
    def __init__(self, ddb, table: str, write_shards: int, concurrency: int = 32) -> None:
        self._ddb = ddb
        self._table = table
        self._shards = write_shards
        self._sem = asyncio.Semaphore(concurrency)

    def _put(self, msg: QueueMessage, event: Event) -> bool:
        tenant = str(msg.tenant_id)
        pk, sk = item_keys(tenant, event, self._shards)
        try:
            self._ddb.put_item(
                TableName=self._table,
                Item={
                    "pk": {"S": pk},
                    "sk": {"S": sk},
                    "tenant_id": {"S": tenant},
                    "event_id": {"S": str(event.event_id)},
                    "name": {"S": event.name},
                    "distinct_id": {"S": event.distinct_id},
                    # JSON string: DynamoDB rejects Python floats; we never filter on it here.
                    "properties": {"S": event.model_dump_json(include={"properties"})},
                    "occurred_at": {"S": event.occurred_at.astimezone(UTC).isoformat()},
                    "received_at": {"S": msg.received_at.isoformat()},
                    "batch_id": {"S": str(msg.batch_id)},
                },
                ConditionExpression="attribute_not_exists(pk) OR batch_id = :b",
                ExpressionAttributeValues={":b": {"S": str(msg.batch_id)}},
            )
            return True
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise

    async def put_batch(self, msg: QueueMessage) -> int:
        """Write every event in the message; return how many this batch owns."""

        async def one(e: Event) -> bool:
            async with self._sem:
                return await asyncio.to_thread(self._put, msg, e)

        # Dedupe within the message first so two copies can't both "own" an event.
        unique = list({e.event_id: e for e in msg.events}.values())
        results = await asyncio.gather(*(one(e) for e in unique))
        return sum(results)

    def _query_shard(self, pk: str) -> list[dict]:
        items, kwargs = [], {}
        while True:
            resp = self._ddb.query(
                TableName=self._table,
                KeyConditionExpression="pk = :pk",
                ExpressionAttributeValues={":pk": {"S": pk}},
                **kwargs,
            )
            items.extend(resp["Items"])
            if "LastEvaluatedKey" not in resp:
                return items
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

    async def query_day(self, tenant_id: str, day: date) -> list[dict]:
        """Scatter to every write shard in parallel, then merge by sort key."""
        pks = [f"{tenant_id}#{day.isoformat()}#{s}" for s in range(self._shards)]
        per_shard = await asyncio.gather(*(asyncio.to_thread(self._query_shard, pk) for pk in pks))
        return list(heapq.merge(*per_shard, key=lambda it: it["sk"]["S"]))
