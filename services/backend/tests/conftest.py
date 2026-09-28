"""Integration-test fixtures. These hit a real Postgres and Redis (docker compose
or CI service containers), because mocking a database hides exactly the bugs
(constraint behaviour, ON CONFLICT semantics, Lua atomicity) we care about."""

import asyncio
import os
import urllib.request
from uuid import UUID

import asyncpg
import pytest
from fastapi.testclient import TestClient
from moto.server import ThreadedMotoServer
from redis import Redis

from app.auth import generate_api_key
from app.aws_setup import ensure_resources
from app.config import Settings
from app.main import create_app
from app.migrate import migrate

# Dummy credentials so boto3 never touches a real AWS account from tests.
# (boto3 reads these when a client is created, so setting them here is early enough.)
for _k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
    os.environ[_k] = "testing"

DB_URL = os.getenv("TALLY_TEST_DATABASE_URL", "postgresql://tally:tally@localhost:5432/tally_test")
REDIS_URL = os.getenv("TALLY_TEST_REDIS_URL", "redis://localhost:6379/15")


def run(coro):
    return asyncio.run(coro)


async def _sql(query: str, *args):
    conn = await asyncpg.connect(DB_URL)
    try:
        return await conn.fetch(query, *args)
    finally:
        await conn.close()


def sql(query: str, *args):
    """Run a query from sync test code on a dedicated connection."""
    return run(_sql(query, *args))


@pytest.fixture(scope="session", autouse=True)
def schema():
    async def rebuild():
        conn = await asyncpg.connect(DB_URL)
        try:
            await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        finally:
            await conn.close()
        await migrate(DB_URL)  # the same runner production uses

    run(rebuild())


@pytest.fixture(autouse=True)
def clean_state():
    sql("TRUNCATE tenants, users, api_keys, events, usage_daily, usage_ledger, sketches CASCADE")
    Redis.from_url(REDIS_URL).flushdb()


def make_tenant(rate: int = 1000, burst: int = 1000) -> tuple[UUID, str]:
    tenant_id = sql(
        "INSERT INTO tenants (name, rate_limit_per_sec, rate_limit_burst) VALUES ('Acme', $1, $2) RETURNING id",
        rate,
        burst,
    )[0]["id"]
    plaintext, prefix, digest = generate_api_key()
    sql(
        "INSERT INTO api_keys (tenant_id, name, key_prefix, key_hash) VALUES ($1, 'test', $2, $3)",
        tenant_id,
        prefix,
        digest,
    )
    return tenant_id, plaintext


@pytest.fixture
def tenant():
    return make_tenant()


@pytest.fixture
def client():
    settings = Settings(database_url=DB_URL, redis_url=REDIS_URL, api_key_cache_ttl_sec=0)
    with TestClient(create_app(settings)) as c:
        yield c


# --- AWS (moto) ---------------------------------------------------------------
MOTO_PORT = int(os.getenv("TALLY_TEST_MOTO_PORT", "5055"))


@pytest.fixture(scope="session")
def moto_url():
    server = ThreadedMotoServer(port=MOTO_PORT, verbose=False)
    server.start()
    yield f"http://127.0.0.1:{MOTO_PORT}"
    server.stop()


@pytest.fixture
def aws_settings(moto_url):
    """Fresh queues + table per test, configured for fast, deterministic retries."""
    urllib.request.urlopen(urllib.request.Request(f"{moto_url}/moto-api/reset", method="POST"))
    st = Settings(
        database_url=DB_URL,
        redis_url=REDIS_URL,
        api_key_cache_ttl_sec=0,
        sink="sqs",
        aws_endpoint_url=moto_url,
        aws_region="us-east-1",
        queue_shards=4,
        worker_wait_seconds=0,  # no long polling in tests
        worker_visibility_timeout=0,  # failed messages reappear immediately
        max_receive_count=2,
    )
    ensure_resources(st)
    return st


@pytest.fixture
def sqs_client(aws_settings):
    with TestClient(create_app(aws_settings)) as c:
        yield c
