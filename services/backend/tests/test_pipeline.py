"""End-to-end tests for the async pipeline: API -> SQS -> worker -> DynamoDB + Postgres.

The headline property: billing is exact under every failure mode we can
provoke: redelivery, a worker crash mid-message, client retries, and poison
messages.
"""

import asyncio
import json
import random
import uuid
from datetime import UTC, date, datetime, timedelta

import asyncpg
import pytest

from app import aws
from app import worker as worker_mod
from app.hashring import ConsistentHashRing
from app.models import Event, QueueMessage
from app.store import DynamoEventStore
from app.worker import Worker

from .conftest import make_tenant, sql
from .test_api import auth, event


# --- helpers -------------------------------------------------------------------
async def _with_worker(settings, fn):
    pool = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=2)
    try:
        w = Worker(settings, aws.client("sqs", settings), store(settings), pool)
        return await fn(w)
    finally:
        await pool.close()


def store(settings):
    return DynamoEventStore(aws.client("dynamodb", settings), settings.dynamodb_table, settings.write_shards)


def drain(settings, rounds: int = 20) -> int:
    """Poll every shard until a full pass finds nothing to do."""

    async def go(w):
        sqs = aws.client("sqs", settings)
        urls = [sqs.get_queue_url(QueueName=n)["QueueUrl"] for n in settings.queue_names()]
        total = 0
        for _ in range(rounds):
            handled = sum([await w.poll_once(u) for u in urls])
            total += handled
            if handled == 0:
                break
        return total

    return asyncio.run(_with_worker(settings, go))


def process(settings, body: str) -> int:
    return asyncio.run(_with_worker(settings, lambda w: w.process(body)))


def stored(settings, tenant_id, day=None) -> list[dict]:
    return asyncio.run(store(settings).query_day(str(tenant_id), day or datetime.now(UTC).date()))


def usage(tenant_id) -> int:
    rows = sql("SELECT coalesce(sum(event_count), 0) FROM usage_daily WHERE tenant_id = $1", tenant_id)
    return rows[0][0]


def sketch_event_total(tenant_id) -> int:
    from tally_sketches import TopK

    rows = sql("SELECT data FROM sketches WHERE tenant_id = $1 AND kind = 'events'", tenant_id)
    return sum(TopK.from_bytes(r["data"]).total for r in rows)


def queue_depths(settings) -> dict[str, int]:
    sqs = aws.client("sqs", settings)
    out = {}
    for n in [*settings.queue_names(), settings.dlq_name]:
        url = sqs.get_queue_url(QueueName=n)["QueueUrl"]
        attrs = sqs.get_queue_attributes(
            QueueUrl=url,
            AttributeNames=["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"],
        )["Attributes"]
        out[n] = int(attrs["ApproximateNumberOfMessages"]) + int(
            attrs["ApproximateNumberOfMessagesNotVisible"]
        )
    return out


def message(tenant_id, events, batch_id=None) -> str:
    return QueueMessage(
        batch_id=batch_id or uuid.uuid4(),
        tenant_id=tenant_id,
        received_at=datetime.now(UTC),
        events=[Event.model_validate(e) for e in events],
    ).model_dump_json()


# --- tests ---------------------------------------------------------------------
def test_api_enqueues_on_the_tenants_shard_only(sqs_client, aws_settings):
    tenant_id, key = make_tenant()
    r = sqs_client.post("/v1/events", json={"events": [event() for _ in range(3)]}, headers=auth(key))
    assert r.status_code == 202
    assert r.json() == {"accepted": 3, "duplicates": None}

    expected = ConsistentHashRing(aws_settings.queue_names()).get(str(tenant_id))
    depths = queue_depths(aws_settings)
    assert depths[expected] == 1
    assert sum(v for k, v in depths.items() if k != expected) == 0


def test_end_to_end_events_are_stored_and_billed(sqs_client, aws_settings):
    tenant_id, key = make_tenant()
    sqs_client.post("/v1/events", json={"events": [event() for _ in range(5)]}, headers=auth(key))
    assert drain(aws_settings) == 1
    assert len(stored(aws_settings, tenant_id)) == 5
    assert usage(tenant_id) == 5
    assert sum(queue_depths(aws_settings).values()) == 0  # message deleted after success


def test_redelivered_message_is_billed_once(aws_settings):
    tenant_id, _ = make_tenant()
    body = message(tenant_id, [event() for _ in range(3)])
    assert process(aws_settings, body) == 3
    assert process(aws_settings, body) == 3  # same batch re-owns its own events...
    assert usage(tenant_id) == 3  # ...but the ledger applies usage only once
    assert sketch_event_total(tenant_id) == 3  # and the sketches, which are additive, only once
    assert len(stored(aws_settings, tenant_id)) == 3


def test_crash_after_dynamo_before_billing_recovers_exactly(aws_settings, monkeypatch):
    """The classic at-least-once trap: events are written, then the worker dies
    before recording usage. On redelivery every event already exists. A naive
    'count newly inserted rows' would bill 0 forever. We must still bill 4."""
    tenant_id, _ = make_tenant()
    body = message(tenant_id, [event() for _ in range(4)])

    real = worker_mod.commit_batches

    async def crash(*a, **kw):
        raise ConnectionError("simulated crash before billing")

    monkeypatch.setattr(worker_mod, "commit_batches", crash)
    with pytest.raises(ConnectionError):
        process(aws_settings, body)
    assert len(stored(aws_settings, tenant_id)) == 4 and usage(tenant_id) == 0

    monkeypatch.setattr(worker_mod, "commit_batches", real)
    process(aws_settings, body)  # the redelivery
    assert usage(tenant_id) == 4
    assert sketch_event_total(tenant_id) == 4  # sketches recovered too


def test_client_retry_in_a_new_batch_is_not_billed_twice(aws_settings):
    tenant_id, _ = make_tenant()
    events = [event() for _ in range(3)]
    assert process(aws_settings, message(tenant_id, events)) == 3
    assert process(aws_settings, message(tenant_id, events)) == 0  # different batch_id -> true dupes
    assert usage(tenant_id) == 3


def test_duplicates_inside_one_message_count_once(aws_settings):
    tenant_id, _ = make_tenant()
    e = event()
    assert process(aws_settings, message(tenant_id, [e, e, event()])) == 2


def test_poison_message_lands_in_dlq_and_does_not_block_the_shard(aws_settings):
    tenant_id, key = make_tenant()
    sqs = aws.client("sqs", aws_settings)
    shard = ConsistentHashRing(aws_settings.queue_names()).get(str(tenant_id))
    url = sqs.get_queue_url(QueueName=shard)["QueueUrl"]
    sqs.send_message(QueueUrl=url, MessageBody="{not valid json")
    sqs.send_message(QueueUrl=url, MessageBody=message(tenant_id, [event() for _ in range(2)]))

    drain(aws_settings)
    drain(aws_settings)  # redrive happens on the receive after maxReceiveCount
    depths = queue_depths(aws_settings)
    assert depths[aws_settings.dlq_name] == 1
    assert depths[shard] == 0
    assert usage(tenant_id) == 2  # the good message behind it still went through


def test_large_request_is_split_across_messages(sqs_client, aws_settings):
    tenant_id, key = make_tenant()
    events = [event(properties={"blob": "x" * 1_000}) for _ in range(500)]
    assert sqs_client.post("/v1/events", json={"events": events}, headers=auth(key)).status_code == 202
    assert drain(aws_settings) >= 3  # ~500 KB of events -> several ≤200 KB messages
    assert usage(tenant_id) == 500
    assert len(stored(aws_settings, tenant_id)) == 500


def test_writes_spread_across_shards_and_reads_merge_in_time_order(aws_settings):
    tenant_id, _ = make_tenant()
    day = date(2026, 3, 1)
    base = datetime(2026, 3, 1, tzinfo=UTC)
    times = [base + timedelta(seconds=random.randint(0, 86_399)) for _ in range(60)]
    process(aws_settings, message(tenant_id, [event(occurred_at=t.isoformat()) for t in times]))

    items = stored(aws_settings, tenant_id, day)
    assert [it["occurred_at"]["S"] for it in items] == sorted(t.isoformat() for t in times)
    assert len({it["pk"]["S"] for it in items}) > 1  # hot-partition avoidance actually engaged
    assert json.loads(items[0]["properties"]["S"]) == {"properties": {"path": "/pricing"}}


def test_mixed_timezone_offsets_still_sort_chronologically(aws_settings):
    """10:30+02:00 is 08:30 UTC, so it must sort before 09:00Z even though '10' > '09' as text."""
    tenant_id, _ = make_tenant()
    process(
        aws_settings,
        message(
            tenant_id,
            [
                event(name="second", occurred_at="2026-03-01T09:00:00+00:00"),
                event(name="first", occurred_at="2026-03-01T10:30:00+02:00"),
            ],
        ),
    )
    names = [it["name"]["S"] for it in stored(aws_settings, tenant_id, date(2026, 3, 1))]
    assert names == ["first", "second"]


def test_readyz_checks_sqs(sqs_client):
    assert sqs_client.get("/readyz").json() == {"postgres": "ok", "redis": "ok", "sqs": "ok"}
