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
    # Known immediately in postgres mode. In sqs mode dedupe happens later in the
    # worker, so this is null and `accepted` means "durably enqueued".
    duplicates: int | None


class QueueMessage(BaseModel):
    """The envelope the ingest tier puts on SQS and workers consume.

    batch_id is generated once per message and survives redelivery unchanged,
    which is what lets workers make processing exactly-once in effect.
    received_at is the ingest server's clock, so billing day is deterministic.
    """

    batch_id: UUID
    tenant_id: UUID
    received_at: datetime
    events: list[Event]
