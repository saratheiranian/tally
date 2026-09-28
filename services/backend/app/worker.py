"""Queue worker:  python -m app.worker

For each message: write events to DynamoDB, then record usage in Postgres, then
delete the message. If anything fails the message is *not* deleted; SQS makes it
visible again after the visibility timeout and another attempt runs. After
max_receive_count failures, SQS's redrive policy moves it to the dead-letter
queue, so a poison message can't block a shard forever.

Every step is idempotent (see store.py and usage.py), so a crash between any
two steps is safe: the retry converges on the same final state.
"""

import asyncio
import logging
import signal
from datetime import UTC

import asyncpg

from . import aws
from .config import Settings
from .config import settings as default_settings
from .models import QueueMessage
from .store import DynamoEventStore
from .usage import record_usage

log = logging.getLogger("tally.worker")


class Worker:
    def __init__(self, settings: Settings, sqs, store: DynamoEventStore, pool: asyncpg.Pool) -> None:
        self.settings = settings
        self.sqs = sqs
        self.store = store
        self.pool = pool

    async def process(self, body: str) -> int:
        """Handle one message body. Returns events owned (billed) by this batch."""
        msg = QueueMessage.model_validate_json(body)
        owned = await self.store.put_batch(msg)
        day = msg.received_at.astimezone(UTC).date()
        await record_usage(self.pool, msg.batch_id, msg.tenant_id, day, owned)
        log.info(
            "batch=%s tenant=%s events=%d billed=%d",
            msg.batch_id,
            msg.tenant_id,
            len(msg.events),
            owned,
        )
        return owned

    async def _handle(self, m: dict) -> str | None:
        try:
            await self.process(m["Body"])
            return m["ReceiptHandle"]
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
        receipts = await asyncio.gather(*(self._handle(m) for m in messages))
        done = [r for r in receipts if r]
        if done:
            await asyncio.to_thread(
                self.sqs.delete_message_batch,
                QueueUrl=queue_url,
                Entries=[{"Id": str(i), "ReceiptHandle": r} for i, r in enumerate(done)],
            )
        return len(done)

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
