import uuid
from datetime import UTC, datetime

import pytest

from app.models import Event
from app.queueing import EventTooLarge, chunk_events, pack_bodies


def ev(payload_bytes=0):
    return Event(
        event_id=uuid.uuid4(),
        name="e",
        distinct_id="u",
        properties={"blob": "x" * payload_bytes},
        occurred_at=datetime.now(UTC),
    )


def test_chunks_respect_size_limit_and_keep_order():
    events = [ev(900) for _ in range(50)]
    chunks = chunk_events(events, limit=10_000)
    assert len(chunks) > 1
    assert [e for c in chunks for e in c] == events
    for c in chunks:
        assert sum(len(e.model_dump_json()) + 1 for e in c) <= 10_000


def test_single_oversized_event_is_rejected():
    with pytest.raises(EventTooLarge):
        chunk_events([ev(20_000)], limit=10_000)


def test_pack_respects_entry_and_byte_limits():
    groups = pack_bodies(["a" * 100] * 25, max_bytes=10_000, max_entries=10)
    assert [len(g) for g in groups] == [10, 10, 5]
    groups = pack_bodies(["a" * 4_000] * 5, max_bytes=10_000, max_entries=10)
    assert [len(g) for g in groups] == [2, 2, 1]
