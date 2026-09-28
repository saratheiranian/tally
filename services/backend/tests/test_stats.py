"""Sketch-backed statistics: accuracy against exact answers, exactly-once
absorption, cross-day merging, and lock correctness under concurrency."""

import asyncio
import random
import uuid
from datetime import UTC, date, datetime, timedelta

import asyncpg

from app.aggregates import ProcessedBatch, commit_batches
from app.models import Event

from .conftest import make_tenant, sql
from .test_api import auth, event
from .test_pipeline import message, process

DAY = date(2026, 3, 2)


def realistic_events(n, users, day=DAY, seed=5):
    rng = random.Random(seed)
    pages = [f"/p/{i}" for i in range(200)]
    weights = [1 / (r**1.1) for r in range(1, 201)]
    base = datetime(day.year, day.month, day.day, tzinfo=UTC)
    out = []
    for _ in range(n):
        name = rng.choices(["page_view", "signup", "purchase", "search"], [80, 5, 3, 12])[0]
        props = {"path": rng.choices(pages, weights)[0]} if name == "page_view" else {}
        out.append(
            event(
                name=name,
                distinct_id=f"user-{rng.randrange(users)}",
                properties=props,
                occurred_at=(base + timedelta(seconds=rng.randrange(86_400))).isoformat(),
            )
        )
    return out


def ingest(settings, tenant_id, events, per_message=250):
    for i in range(0, len(events), per_message):
        process(settings, message(tenant_id, events[i : i + per_message]))


def test_approximate_stats_track_exact_answers(sqs_client, aws_settings):
    tenant_id, key = make_tenant()
    ingest(aws_settings, tenant_id, realistic_events(4_000, users=1_500))
    q = f"/v1/stats?start={DAY}&end={DAY}&limit=5"
    approx = sqs_client.get(q, headers=auth(key)).json()
    exact = sqs_client.get(q + "&exact=true", headers=auth(key)).json()

    assert approx["approximate"] is True and exact["approximate"] is False
    assert approx["events"] == exact["events"] == 4_000  # sketch totals are exact counters
    assert abs(approx["unique_users"] - exact["unique_users"]) / exact["unique_users"] < 0.03
    assert [r["key"] for r in approx["top_pages"]] == [r["key"] for r in exact["top_pages"]]
    assert [r["key"] for r in approx["top_events"]] == [r["key"] for r in exact["top_events"]]
    for a, e in zip(approx["top_pages"], exact["top_pages"], strict=True):
        assert a["count"] >= e["count"]  # Count-Min never underestimates


def test_unique_users_across_days_is_a_union_not_a_sum(sqs_client, aws_settings):
    tenant_id, key = make_tenant()
    d1, d2 = DAY, DAY + timedelta(days=1)
    # 600 users on day 1, 600 on day 2, 400 of them the same people -> 800 unique.
    ingest(
        aws_settings,
        tenant_id,
        [event(distinct_id=f"u{i}", occurred_at=f"{d1}T12:00:00+00:00") for i in range(600)],
    )
    ingest(
        aws_settings,
        tenant_id,
        [event(distinct_id=f"u{i}", occurred_at=f"{d2}T12:00:00+00:00") for i in range(200, 800)],
    )
    r = sqs_client.get(f"/v1/stats?start={d1}&end={d2}", headers=auth(key)).json()
    assert r["events"] == 1_200
    assert abs(r["unique_users"] - 800) / 800 < 0.03  # a naive sum of daily counts would say 1,200


def test_events_are_bucketed_by_occurred_day(sqs_client, aws_settings):
    tenant_id, key = make_tenant()
    late = event(occurred_at=f"{DAY}T23:59:59+00:00")
    early = event(occurred_at=f"{DAY + timedelta(days=1)}T00:00:00+00:00")
    process(aws_settings, message(tenant_id, [late, early]))
    one_day = sqs_client.get(f"/v1/stats?start={DAY}&end={DAY}", headers=auth(key)).json()
    assert one_day["events"] == 1


def test_concurrent_commits_to_one_sketch_lose_nothing(aws_settings):
    """Twenty transactions race to update the same tenant-day sketch rows, as can
    happen briefly during a shard rebalance. Placeholder rows plus ordered
    FOR UPDATE locks must serialise them: no lost updates, no deadlocks.

    Each batch gets a *different* billing day. Otherwise the usage_daily upsert
    row-locks one shared row and serialises the transactions by accident, and
    the test passes even with the sketch locks deleted (verified by mutation).
    In production, billing day (received) and sketch day (occurred) often differ
    (late mobile events, backfills), so that accidental lock can't be relied on."""
    tenant_id, _ = make_tenant()
    ts = datetime(DAY.year, DAY.month, DAY.day, 12, tzinfo=UTC)

    def batch(i):
        evs = [
            Event(
                event_id=uuid.uuid4(),
                name="page_view",
                distinct_id=f"u{i}-{j}",
                properties={"path": "/"},
                occurred_at=ts,
            )
            for j in range(10)
        ]
        return ProcessedBatch(uuid.uuid4(), tenant_id, DAY + timedelta(days=i), evs)

    async def race():
        pool = await asyncpg.create_pool(aws_settings.database_url, min_size=10, max_size=20)
        try:
            await asyncio.gather(
                *(commit_batches(pool, [batch(i)], aws_settings.sketch_config()) for i in range(20))
            )
        finally:
            await pool.close()

    asyncio.run(race())
    from tally_sketches import HyperLogLog, TopK

    rows = {
        r["kind"]: r["data"] for r in sql("SELECT kind, data FROM sketches WHERE tenant_id = $1", tenant_id)
    }
    assert TopK.from_bytes(rows["events"]).total == 200
    assert abs(HyperLogLog.from_bytes(rows["users"]).estimate() - 200) < 6
    assert sql("SELECT sum(event_count) FROM usage_daily WHERE tenant_id = $1", tenant_id)[0][0] == 200


def test_duplicate_message_in_one_poll_is_applied_once(aws_settings):
    tenant_id, _ = make_tenant()
    ts = datetime(DAY.year, DAY.month, DAY.day, tzinfo=UTC)
    b = ProcessedBatch(
        uuid.uuid4(),
        tenant_id,
        DAY,
        [Event(event_id=uuid.uuid4(), name="x", distinct_id="u", occurred_at=ts)],
    )

    async def go():
        pool = await asyncpg.create_pool(aws_settings.database_url, min_size=1, max_size=2)
        try:
            await commit_batches(pool, [b, b], aws_settings.sketch_config())
        finally:
            await pool.close()

    asyncio.run(go())
    assert sql("SELECT event_count FROM usage_daily WHERE tenant_id = $1", tenant_id)[0][0] == 1


def test_postgres_mode_uses_exact_sql(client, tenant):
    _, key = tenant
    evs = [
        event(distinct_id="a", properties={"path": "/x"}),
        event(distinct_id="a", properties={"path": "/x"}),
        event(distinct_id="b", properties={"path": "/y"}),
        event(name="signup", distinct_id="b", properties={}),
    ]
    client.post("/v1/events", json={"events": evs}, headers=auth(key))
    today = datetime.now(UTC).date()
    r = client.get(f"/v1/stats?start={today}&end={today}", headers=auth(key)).json()
    assert r["approximate"] is False
    assert (r["events"], r["unique_users"]) == (4, 2)
    assert r["top_pages"] == [{"key": "/x", "count": 2}, {"key": "/y", "count": 1}]
    assert r["top_events"][0] == {"key": "page_view", "count": 3}


def test_range_validation(sqs_client, aws_settings):
    _, key = make_tenant()
    h = auth(key)
    assert sqs_client.get("/v1/stats?start=2026-03-02&end=2026-03-01", headers=h).status_code == 422
    assert sqs_client.get("/v1/stats?start=2025-01-01&end=2026-03-01", headers=h).status_code == 422
    assert (
        sqs_client.get("/v1/stats?start=2026-01-01&end=2026-03-01&exact=true", headers=h).status_code == 422
    )
    assert sqs_client.get("/v1/stats?start=2026-01-01&end=2026-03-01", headers=h).status_code == 200
    assert sqs_client.get("/v1/stats?start=2026-03-01&end=2026-03-01").status_code == 401
