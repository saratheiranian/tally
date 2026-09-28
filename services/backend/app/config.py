from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All config comes from environment variables (12-factor)."""

    model_config = SettingsConfigDict(env_prefix="TALLY_", env_file=".env", extra="ignore")

    database_url: str = "postgresql://tally:tally@localhost:5432/tally"
    redis_url: str = "redis://localhost:6379/0"
    max_batch_size: int = 500
    api_key_cache_ttl_sec: float = 30.0
    db_pool_min: int = 2
    db_pool_max: int = 10


settings = Settings()
