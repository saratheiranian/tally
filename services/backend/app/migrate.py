"""Apply SQL migrations in order:  python -m app.migrate

* Each file in db/migrations runs once, in its own transaction, and is recorded
  in schema_migrations with a SHA-256 checksum.
* Editing a migration after it has been applied is an error, not a silent skip:
  environments must never quietly diverge.
* A Postgres advisory lock serialises concurrent runners. In ECS, two deploys (or
  a deploy racing a manual run) can't apply the same migration twice.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import pathlib

import asyncpg

from .config import settings

log = logging.getLogger("tally.migrate")
LOCK_KEY = 0x7A11_7A11  # arbitrary, stable app-wide advisory lock id

_default_dir = pathlib.Path(__file__).resolve().parents[3] / "db" / "migrations"
MIGRATIONS_DIR = pathlib.Path(os.getenv("TALLY_MIGRATIONS_DIR", _default_dir))


class ChecksumMismatch(RuntimeError):
    pass


async def migrate(dsn: str, directory: pathlib.Path = MIGRATIONS_DIR) -> list[str]:
    """Returns the versions applied by this run."""
    files = sorted(directory.glob("*.sql"))
    if not files:
        raise FileNotFoundError(f"no migrations found in {directory}")
    conn = await asyncpg.connect(dsn)
    applied_now: list[str] = []
    try:
        await conn.execute("SELECT pg_advisory_lock($1)", LOCK_KEY)
        await conn.execute(
            """CREATE TABLE IF NOT EXISTS schema_migrations (
                   version    TEXT PRIMARY KEY,
                   checksum   TEXT NOT NULL,
                   applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"""
        )
        done = {
            r["version"]: r["checksum"]
            for r in await conn.fetch("SELECT version, checksum FROM schema_migrations")
        }
        for f in files:
            sql = f.read_text()
            checksum = hashlib.sha256(sql.encode()).hexdigest()
            if f.name in done:
                if done[f.name] != checksum:
                    raise ChecksumMismatch(
                        f"{f.name} changed after it was applied; add a new migration instead"
                    )
                continue
            async with conn.transaction():
                await conn.execute(sql)
                await conn.execute(
                    "INSERT INTO schema_migrations (version, checksum) VALUES ($1, $2)", f.name, checksum
                )
            log.info("applied %s", f.name)
            applied_now.append(f.name)
    finally:
        await conn.execute("SELECT pg_advisory_unlock($1)", LOCK_KEY)
        await conn.close()
    return applied_now


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    applied = asyncio.run(migrate(settings.database_url))
    log.info("done: %d applied", len(applied)) if applied else log.info("up to date")
