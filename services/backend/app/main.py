import asyncio
import math
from contextlib import asynccontextmanager
from datetime import date
from typing import Annotated

import asyncpg
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response, status
from redis.asyncio import Redis

from . import aws
from .auth import ApiKeyAuthenticator, Tenant
from .config import Settings
from .config import settings as default_settings
from .hashring import ConsistentHashRing
from .models import EventBatch, IngestResult
from .queueing import EventTooLarge
from .ratelimit import TokenBucketLimiter
from .sink import PostgresSink, SinkUnavailable, SqsSink
from .stats import Stats, approximate_stats, exact_stats_dynamo, exact_stats_postgres
from .store import DynamoEventStore


def create_app(settings: Settings = default_settings) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        pool = await asyncpg.create_pool(
            settings.database_url, min_size=settings.db_pool_min, max_size=settings.db_pool_max
        )
        redis = Redis.from_url(settings.redis_url)
        app.state.pool = pool
        app.state.redis = redis
        app.state.auth = ApiKeyAuthenticator(pool, ttl=settings.api_key_cache_ttl_sec)
        app.state.limiter = TokenBucketLimiter(redis)
        if settings.sink == "sqs":
            sqs = aws.client("sqs", settings)
            urls = {n: sqs.get_queue_url(QueueName=n)["QueueUrl"] for n in settings.queue_names()}
            app.state.sqs = sqs
            app.state.sink = SqsSink(sqs, urls, ConsistentHashRing(urls))
            app.state.readiness_queue_url = next(iter(urls.values()))
            app.state.store = DynamoEventStore(
                aws.client("dynamodb", settings), settings.dynamodb_table, settings.write_shards
            )
        else:
            app.state.sink = PostgresSink(pool)
        try:
            yield
        finally:
            await redis.aclose()
            await pool.close()

    app = FastAPI(title="Tally Ingest API", version="0.1.0", lifespan=lifespan)

    async def current_tenant(
        request: Request, authorization: Annotated[str | None, Header()] = None
    ) -> Tenant:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token")
        tenant = await request.app.state.auth.authenticate(authorization.removeprefix("Bearer "))
        if tenant is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid API key")
        return tenant

    @app.post("/v1/events", response_model=IngestResult, status_code=status.HTTP_202_ACCEPTED)
    async def ingest(
        batch: EventBatch,
        request: Request,
        response: Response,
        tenant: Annotated[Tenant, Depends(current_tenant)],
    ) -> IngestResult:
        n = len(batch.events)
        if n > settings.max_batch_size:
            raise HTTPException(
                status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                f"Batch of {n} exceeds max_batch_size={settings.max_batch_size}",
            )

        # Cost = number of events, so one 500-event batch is charged like 500 calls.
        decision = await request.app.state.limiter.acquire(
            bucket=str(tenant.id),
            rate=tenant.rate_limit_per_sec,
            burst=tenant.rate_limit_burst,
            cost=n,
        )
        if not decision.allowed:
            if decision.retry_after_ms < 0:
                raise HTTPException(
                    status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    f"Batch of {n} exceeds your burst limit of {tenant.rate_limit_burst}; split it",
                )
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                "Rate limit exceeded",
                headers={"Retry-After": str(math.ceil(decision.retry_after_ms / 1000))},
            )
        response.headers["X-RateLimit-Remaining"] = str(int(decision.remaining))
        try:
            return await request.app.state.sink.write(tenant.id, batch.events)
        except EventTooLarge as exc:
            raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, str(exc)) from exc
        except SinkUnavailable as exc:
            # Safe for the client to resend the whole batch: workers dedupe by event_id.
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, str(exc), headers={"Retry-After": "1"}
            ) from exc

    @app.get("/v1/stats", response_model=Stats)
    async def stats(
        request: Request,
        tenant: Annotated[Tenant, Depends(current_tenant)],
        start: date,
        end: date,
        exact: bool = False,
        limit: Annotated[int, Query(ge=1, le=20)] = 10,
    ) -> Stats:
        """Events, unique users, and top events/pages for [start, end] (UTC days, inclusive)."""
        if end < start:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "end must be on or after start")
        days = (end - start).days + 1
        pipeline = settings.sink == "sqs"
        # Sketch merges cost O(days); exact scans cost O(events), so they get a tighter cap.
        max_days = 366 if pipeline and not exact else 31
        if days > max_days:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"range is {days} days; max is {max_days} for this query",
            )
        pool = request.app.state.pool
        if not pipeline:
            return await exact_stats_postgres(pool, tenant.id, start, end, limit)
        if exact:
            return await exact_stats_dynamo(request.app.state.store, tenant.id, start, end, limit)
        return await approximate_stats(pool, tenant.id, start, end, settings.sketch_config(), limit)

    @app.get("/healthz")
    async def healthz() -> dict:
        """Liveness: the process is up. Never touches dependencies."""
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz(request: Request, response: Response) -> dict:
        """Readiness: dependencies reachable. The load balancer uses this."""
        checks = {}
        try:
            await request.app.state.pool.fetchval("SELECT 1")
            checks["postgres"] = "ok"
        except Exception as exc:  # noqa: BLE001
            checks["postgres"] = f"error: {exc.__class__.__name__}"
        try:
            await request.app.state.redis.ping()
            checks["redis"] = "ok"
        except Exception as exc:  # noqa: BLE001
            checks["redis"] = f"error: {exc.__class__.__name__}"
        if settings.sink == "sqs":
            try:
                # Scoped to our own queue, so the task role needs no account-wide sqs:ListQueues.
                await asyncio.to_thread(
                    request.app.state.sqs.get_queue_attributes,
                    QueueUrl=request.app.state.readiness_queue_url,
                    AttributeNames=["QueueArn"],
                )
                checks["sqs"] = "ok"
            except Exception as exc:  # noqa: BLE001
                checks["sqs"] = f"error: {exc.__class__.__name__}"
        if any(v != "ok" for v in checks.values()):
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return checks

    return app


app = create_app()
