from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All config comes from environment variables (12-factor)."""

    model_config = SettingsConfigDict(env_prefix="TALLY_", env_file=".env", extra="ignore")

    # --- Postgres / Redis -----------------------------------------------------
    database_url: str = "postgresql://tally:tally@localhost:5432/tally"
    redis_url: str = "redis://localhost:6379/0"
    db_pool_min: int = 2
    db_pool_max: int = 10

    # --- Ingest ---------------------------------------------------------------
    # "postgres": write synchronously (simple mode). "sqs": enqueue for workers.
    sink: Literal["postgres", "sqs"] = "postgres"
    max_batch_size: int = 500
    api_key_cache_ttl_sec: float = 30.0

    # --- AWS (endpoint_url points at LocalStack/moto locally; unset in AWS) ----
    aws_region: str = "eu-west-2"
    aws_endpoint_url: str | None = None

    # --- Queues ---------------------------------------------------------------
    queue_prefix: str = "tally-events"
    queue_shards: int = 4  # tenants map onto shards via a consistent-hash ring
    dlq_name: str = "tally-events-dlq"
    max_receive_count: int = 5  # deliveries before a message moves to the DLQ

    # --- DynamoDB -------------------------------------------------------------
    dynamodb_table: str = "tally-events"
    write_shards: int = 8  # partition-key suffixes per tenant-day (avoids hot partitions)

    # --- Worker ---------------------------------------------------------------
    worker_shards: str = ""  # e.g. "0,1". Empty means consume every shard.
    worker_wait_seconds: int = 20  # SQS long polling
    worker_visibility_timeout: int = 60
    worker_concurrency: int = 32  # parallel DynamoDB writes per worker

    def queue_names(self) -> list[str]:
        return [f"{self.queue_prefix}-{i}" for i in range(self.queue_shards)]

    def worker_queue_names(self) -> list[str]:
        if not self.worker_shards.strip():
            return self.queue_names()
        return [f"{self.queue_prefix}-{int(s)}" for s in self.worker_shards.split(",")]


settings = Settings()
