"""Distributed token-bucket rate limiter backed by Redis.

Why a Lua script: the read-refill-decrement cycle must be atomic. If two ingest
nodes did GET then SET separately, both could spend the same tokens. Redis runs
a script as a single operation, so the bucket stays correct across any number of
API instances.

Why redis TIME instead of the caller's clock: API nodes' clocks drift. Using the
Redis server's clock gives every node the same notion of "now".
"""

from dataclasses import dataclass

from redis.asyncio import Redis

TOKEN_BUCKET_LUA = """
local key   = KEYS[1]
local rate  = tonumber(ARGV[1])   -- tokens added per second
local burst = tonumber(ARGV[2])   -- bucket capacity
local cost  = tonumber(ARGV[3])   -- tokens this request needs

local t = redis.call('TIME')
local now_ms = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)

local state  = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(state[1])
local ts     = tonumber(state[2])
if tokens == nil then
  tokens = burst
  ts = now_ms
end

-- Refill based on elapsed time, capped at burst.
local elapsed = math.max(0, now_ms - ts)
tokens = math.min(burst, tokens + elapsed * rate / 1000)

local allowed = 0
local retry_after_ms = 0
if cost <= tokens then
  allowed = 1
  tokens = tokens - cost
elseif cost > burst then
  retry_after_ms = -1  -- can never succeed; caller should split the batch
else
  retry_after_ms = math.ceil((cost - tokens) * 1000 / rate)
end

redis.call('HSET', key, 'tokens', tokens, 'ts', now_ms)
-- Idle buckets expire once they would be full again anyway.
redis.call('PEXPIRE', key, math.ceil(burst * 1000 / rate) + 1000)

return {allowed, tostring(tokens), retry_after_ms}
"""


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    remaining: float
    retry_after_ms: int  # -1 means the request exceeds burst and can never pass


class TokenBucketLimiter:
    def __init__(self, redis: Redis, key_prefix: str = "rl:") -> None:
        self._redis = redis
        self._prefix = key_prefix
        self._script = redis.register_script(TOKEN_BUCKET_LUA)

    async def acquire(self, bucket: str, rate: float, burst: int, cost: int = 1) -> RateLimitDecision:
        allowed, remaining, retry = await self._script(keys=[self._prefix + bucket], args=[rate, burst, cost])
        return RateLimitDecision(bool(allowed), float(remaining), int(retry))
