import asyncio
import pathlib
import shutil

import asyncpg
import pytest

from app.config import Settings
from app.migrate import MIGRATIONS_DIR, ChecksumMismatch, migrate

from .conftest import DB_URL

SCRATCH_DB = "tally_migrate_test"
SCRATCH_URL = DB_URL.rsplit("/", 1)[0] + f"/{SCRATCH_DB}"


@pytest.fixture
def scratch_db():
    async def recreate():
        conn = await asyncpg.connect(DB_URL)
        try:
            await conn.execute(f"DROP DATABASE IF EXISTS {SCRATCH_DB} WITH (FORCE)")
            await conn.execute(f"CREATE DATABASE {SCRATCH_DB}")
        finally:
            await conn.close()

    asyncio.run(recreate())
    yield SCRATCH_URL


def test_applies_all_then_is_idempotent(scratch_db):
    first = asyncio.run(migrate(scratch_db))
    assert first == sorted(p.name for p in MIGRATIONS_DIR.glob("*.sql"))
    assert asyncio.run(migrate(scratch_db)) == []


def test_concurrent_runners_apply_each_migration_once(scratch_db):
    async def race():
        return await asyncio.gather(*(migrate(scratch_db) for _ in range(5)))

    results = asyncio.run(race())
    applied = [v for r in results for v in r]
    assert sorted(applied) == sorted(p.name for p in MIGRATIONS_DIR.glob("*.sql"))


def test_editing_an_applied_migration_is_rejected(scratch_db, tmp_path):
    d = pathlib.Path(tmp_path / "m")
    shutil.copytree(MIGRATIONS_DIR, d)
    asyncio.run(migrate(scratch_db, d))
    (d / "001_init.sql").write_text((d / "001_init.sql").read_text() + "\n-- sneaky edit\n")
    with pytest.raises(ChecksumMismatch):
        asyncio.run(migrate(scratch_db, d))


def test_database_url_can_be_built_from_parts():
    s = Settings(db_host="db.internal", db_user="tally", db_password="p@ss/w:rd", db_name="tally")
    assert s.database_url == "postgresql://tally:p%40ss%2Fw%3Ard@db.internal:5432/tally"
