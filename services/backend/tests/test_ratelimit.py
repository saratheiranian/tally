import asyncio

import pytest
from redis.asyncio import Redis

from app.ratelimit import TokenBucketLimiter

from .conftest import REDIS_URL


@pytest.fixture
async def limiter():
    redis = Redis.from_url(REDIS_URL)
    yield TokenBucketLimiter(redis)
    await redis.aclose()


async def test_allows_up_to_burst_then_denies(limiter):
    for _ in range(5):
        assert (await limiter.acquire("t1", rate=1, burst=5)).allowed
    denied = await limiter.acquire("t1", rate=1, burst=5)
    assert not denied.allowed
    assert denied.retry_after_ms > 0


async def test_cost_larger_than_burst_can_never_pass(limiter):
    d = await limiter.acquire("t2", rate=10, burst=5, cost=6)
    assert not d.allowed and d.retry_after_ms == -1


async def test_refills_over_time(limiter):
    assert (await limiter.acquire("t3", rate=100, burst=10, cost=10)).allowed
    assert not (await limiter.acquire("t3", rate=100, burst=10, cost=5)).allowed
    await asyncio.sleep(0.1)  # 100 tokens/s * 0.1s = 10 tokens
    assert (await limiter.acquire("t3", rate=100, burst=10, cost=5)).allowed


async def test_buckets_are_isolated_per_tenant(limiter):
    assert (await limiter.acquire("a", rate=1, burst=1)).allowed
    assert (await limiter.acquire("b", rate=1, burst=1)).allowed


async def test_atomic_under_concurrency(limiter):
    """200 simultaneous requests against a burst of 20: exactly 20 must pass.
    A non-atomic GET-then-SET limiter over-admits here."""
    results = await asyncio.gather(*(limiter.acquire("hot", rate=0.001, burst=20) for _ in range(200)))
    assert sum(r.allowed for r in results) == 20
