"""Splitting API batches into SQS-sized messages.

SQS limits: 256 KiB per message, 256 KiB total per SendMessageBatch call, and at
most 10 entries per call. One 500-event API request can exceed a single message,
so we chunk events into messages, then pack messages into send calls. Both steps
are greedy bin-filling in arrival order, which is simple and near-optimal
when items are small relative to the bin.
"""

from collections.abc import Sequence

from .models import Event

MAX_MESSAGE_BYTES = 200_000  # headroom under 256 KiB for the envelope
MAX_SEND_BATCH_BYTES = 250_000
MAX_SEND_BATCH_ENTRIES = 10


class EventTooLarge(ValueError):
    pass


def chunk_events(events: Sequence[Event], limit: int = MAX_MESSAGE_BYTES) -> list[list[Event]]:
    chunks: list[list[Event]] = []
    current: list[Event] = []
    size = 0
    for e in events:
        n = len(e.model_dump_json()) + 1  # +1 for the separating comma
        if n > limit:
            raise EventTooLarge(f"event {e.event_id} is {n} bytes; max is {limit}")
        if current and size + n > limit:
            chunks.append(current)
            current, size = [], 0
        current.append(e)
        size += n
    if current:
        chunks.append(current)
    return chunks


def pack_bodies(
    bodies: Sequence[str],
    max_bytes: int = MAX_SEND_BATCH_BYTES,
    max_entries: int = MAX_SEND_BATCH_ENTRIES,
) -> list[list[str]]:
    groups: list[list[str]] = []
    current: list[str] = []
    size = 0
    for b in bodies:
        n = len(b.encode())
        if current and (size + n > max_bytes or len(current) == max_entries):
            groups.append(current)
            current, size = [], 0
        current.append(b)
        size += n
    if current:
        groups.append(current)
    return groups
