"""API-key authentication.

Keys look like `tk_live_<32 random url-safe chars>`. Only SHA-256(key) is stored,
so a database leak does not leak usable credentials. Plain SHA-256 (not bcrypt)
is fine here because keys are high-entropy random strings, not human passwords,
and it keeps per-request auth cheap on the hot path.
"""

import hashlib
import secrets
import time
from dataclasses import dataclass
from uuid import UUID

import asyncpg

KEY_PREFIX = "tk_live_"


@dataclass(frozen=True)
class Tenant:
    id: UUID
    rate_limit_per_sec: int
    rate_limit_burst: int


def hash_key(plaintext: str) -> bytes:
    return hashlib.sha256(plaintext.encode()).digest()


def generate_api_key() -> tuple[str, str, bytes]:
    """Returns (plaintext, display_prefix, hash). Show plaintext to the user once."""
    plaintext = KEY_PREFIX + secrets.token_urlsafe(24)
    return plaintext, plaintext[: len(KEY_PREFIX) + 4], hash_key(plaintext)


class ApiKeyAuthenticator:
    """Looks keys up in Postgres with a small in-process TTL cache.

    Trade-off: a revoked key keeps working on a node for up to `ttl` seconds.
    That is documented and acceptable for ingest; it removes a DB round trip from
    nearly every request.
    """

    def __init__(self, pool: asyncpg.Pool, ttl: float = 30.0, max_entries: int = 10_000) -> None:
        self._pool = pool
        self._ttl = ttl
        self._max = max_entries
        self._cache: dict[bytes, tuple[float, Tenant | None]] = {}

    async def authenticate(self, plaintext: str) -> Tenant | None:
        if not plaintext.startswith(KEY_PREFIX):
            return None
        digest = hash_key(plaintext)
        now = time.monotonic()
        hit = self._cache.get(digest)
        if hit and hit[0] > now:
            return hit[1]

        row = await self._pool.fetchrow(
            """
            SELECT t.id, t.rate_limit_per_sec, t.rate_limit_burst
            FROM api_keys k
            JOIN tenants t ON t.id = k.tenant_id
            WHERE k.key_hash = $1 AND k.revoked_at IS NULL
            """,
            digest,
        )
        tenant = Tenant(**dict(row)) if row else None
        if len(self._cache) >= self._max:
            self._cache.clear()  # crude but bounded; swap for an LRU if it matters
        # Cache misses too (negative caching) so bad keys can't hammer the DB.
        self._cache[digest] = (now + self._ttl, tenant)
        return tenant
