import math
from contextlib import asynccontextmanager
from typing import Annotated

import asyncpg
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, status
from redis.asyncio import Redis

from .auth import ApiKeyAuthenticator, Tenant
from .config import Settings
from .config import settings as default_settings
from .models import EventBatch, IngestResult
from .ratelimit import TokenBucketLimiter
from .sink import PostgresSink


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
        return await request.app.state.sink.write(tenant.id, batch.events)

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
        if any(v != "ok" for v in checks.values()):
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return checks

    return app


app = create_app()
