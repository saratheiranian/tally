"""Queue worker:  python -m app.worker

Each poll receives up to 10 messages and:
  1. writes every message's events to DynamoDB in parallel (conditional puts);
  2. commits billing, usage, and sketch updates for all of them in ONE Postgres
     transaction (aggregates.commit_batches);
  3. deletes the messages from SQS.
If a message fails step 1, it's left out of the commit and not deleted. If the
commit fails, nothing is deleted. SQS redelivers after the visibility timeout,
and after max_receive_count failures the redrive policy moves the message to the
dead-letter queue, so a poison message can't block a shard forever.

Every step is idempotent (see store.py and aggregates.py), so a crash between any
two steps is safe: the retry converges on the same final state.
"""

import asyncio
import logging
import signal
import time
from datetime import UTC

import asyncpg

from . import aws, metrics
from .aggregates import ProcessedBatch, commit_batches
from .config import Settings
from .config import settings as default_settings
from .models import QueueMessage
from .store import DynamoEventStore

log = logging.getLogger("tally.worker")


class Worker:
    def __init__(self, settings: Settings, sqs, store: DynamoEventStore, pool: asyncpg.Pool) -> None:
        self.settings = settings
        self.sqs = sqs
        self.store = store
        self.pool = pool
        self.sketch_cfg = settings.sketch_config()

    async def write(self, body: str) -> ProcessedBatch:
        """Step 1 for one message: store its events and report which it owns."""
        msg = QueueMessage.model_validate_json(body)
        owned = await self.store.put_batch(msg)
        return ProcessedBatch(msg.batch_id, msg.tenant_id, msg.received_at.astimezone(UTC).date(), owned)

    async def process(self, body: str) -> int:
        """Handle a single message end to end. Returns the events billed."""
        batch = await self.write(body)
        await commit_batches(self.pool, [batch], self.sketch_cfg)
        return len(batch.owned)

    async def _write_one(self, m: dict) -> tuple[str, ProcessedBatch] | None:
        try:
            return m["ReceiptHandle"], await self.write(m["Body"])
        except Exception:
            # Leave it on the queue; it will be retried and eventually dead-lettered.
            log.exception("failed message_id=%s (will retry)", m.get("MessageId"))
            return None

    async def poll_once(self, queue_url: str) -> int:
        resp = await asyncio.to_thread(
            self.sqs.receive_message,
            QueueUrl=queue_url,
            MaxNumberOfMessages=10,
            WaitTimeSeconds=self.settings.worker_wait_seconds,
            VisibilityTimeout=self.settings.worker_visibility_timeout,
        )
        messages = resp.get("Messages", [])
        if not messages:
            return 0
        written = [w for w in await asyncio.gather(*(self._write_one(m) for m in messages)) if w]
        if not written:
            return 0
        t0 = time.perf_counter()
        try:
            new_ids = await commit_batches(self.pool, [b for _, b in written], self.sketch_cfg)
        except Exception:
            log.exception("commit failed; %d message(s) will be redelivered", len(written))
            metrics.emit(self._dims(queue_url), {"CommitFailures": (1, "Count")})
            return 0
        commit_ms = (time.perf_counter() - t0) * 1000
        await asyncio.to_thread(
            self.sqs.delete_message_batch,
            QueueUrl=queue_url,
            Entries=[{"Id": str(i), "ReceiptHandle": r} for i, (r, _) in enumerate(written)],
        )
        billed = sum(len(b.owned) for _, b in written if b.batch_id in new_ids)
        redelivered = len(written) - len(new_ids)
        log.info(
            "committed %d message(s) (%d redelivered), %d events billed", len(written), redelivered, billed
        )
        metrics.emit(
            self._dims(queue_url),
            {
                "MessagesCommitted": (len(written), "Count"),
                "MessagesRedelivered": (redelivered, "Count"),
                "EventsBilled": (billed, "Count"),
                "CommitLatency": (commit_ms, "Milliseconds"),
            },
        )
        return len(written)

    @staticmethod
    def _dims(queue_url: str) -> dict[str, str]:
        return {"Queue": queue_url.rsplit("/", 1)[-1]}

    async def run(self, queue_urls: list[str], stop: asyncio.Event) -> None:
        async def loop(url: str) -> None:
            while not stop.is_set():
                try:
                    await self.poll_once(url)
                except Exception:
                    log.exception("poll failed on %s; backing off", url)
                    await asyncio.sleep(1)

        log.info("consuming %d queue(s): %s", len(queue_urls), ", ".join(queue_urls))
        await asyncio.gather(*(loop(u) for u in queue_urls))
        log.info("stopped cleanly")


async def main(settings: Settings = default_settings) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    sqs = aws.client("sqs", settings)
    store = DynamoEventStore(
        aws.client("dynamodb", settings),
        settings.dynamodb_table,
        settings.write_shards,
        settings.worker_concurrency,
    )
    pool = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=settings.db_pool_max)
    urls = [sqs.get_queue_url(QueueName=n)["QueueUrl"] for n in settings.worker_queue_names()]

    # ECS sends SIGTERM on scale-in/deploy. Stop polling, finish in-flight messages,
    # exit. Worst case we wait one long-poll (worker_wait_seconds) — keep that below
    # the task's stopTimeout.
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    try:
        await Worker(settings, sqs, store, pool).run(urls, stop)
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
