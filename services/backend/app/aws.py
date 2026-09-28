import boto3
from botocore.config import Config

from .config import Settings

_BOTO_CONFIG = Config(
    retries={"max_attempts": 5, "mode": "adaptive"},  # backs off on throttling
    max_pool_connections=64,  # >= worker_concurrency, or threads queue on the pool
)


def client(service: str, settings: Settings):
    """boto3 clients are thread-safe, so one per process is shared by all threads."""
    return boto3.session.Session().client(
        service,
        region_name=settings.aws_region,
        endpoint_url=settings.aws_endpoint_url,
        config=_BOTO_CONFIG,
    )
