from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


class Event(BaseModel):
    event_id: UUID = Field(description="Client-generated; makes retries idempotent.")
    name: str = Field(min_length=1, max_length=200)
    distinct_id: str = Field(min_length=1, max_length=200)
    properties: dict[str, Any] = Field(default_factory=dict)
    occurred_at: datetime

    @field_validator("occurred_at")
    @classmethod
    def assume_utc_if_naive(cls, v: datetime) -> datetime:
        # Naive timestamps are ambiguous across a distributed fleet; assume UTC.
        return v if v.tzinfo else v.replace(tzinfo=UTC)


class EventBatch(BaseModel):
    events: list[Event] = Field(min_length=1)


class IngestResult(BaseModel):
    accepted: int
    duplicates: int
