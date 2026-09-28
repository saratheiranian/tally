"""Admin CLI.  Usage:  python -m app.cli create-tenant --name "Acme" [--rate 100 --burst 500]"""

import argparse
import asyncio

import asyncpg

from .auth import generate_api_key
from .config import settings


async def create_tenant(name: str, rate: int, burst: int) -> None:
    conn = await asyncpg.connect(settings.database_url)
    try:
        async with conn.transaction():  # tenant and key are created together or not at all
            tenant_id = await conn.fetchval(
                "INSERT INTO tenants (name, rate_limit_per_sec, rate_limit_burst) VALUES ($1, $2, $3) RETURNING id",
                name,
                rate,
                burst,
            )
            plaintext, prefix, digest = generate_api_key()
            await conn.execute(
                "INSERT INTO api_keys (tenant_id, name, key_prefix, key_hash) VALUES ($1, 'default', $2, $3)",
                tenant_id,
                prefix,
                digest,
            )
    finally:
        await conn.close()
    print(f"tenant_id: {tenant_id}")
    print(f"api_key:   {plaintext}   (shown once; store it now)")


def main() -> None:
    parser = argparse.ArgumentParser(prog="tally")
    sub = parser.add_subparsers(dest="cmd", required=True)
    ct = sub.add_parser("create-tenant")
    ct.add_argument("--name", required=True)
    ct.add_argument("--rate", type=int, default=100)
    ct.add_argument("--burst", type=int, default=500)
    args = parser.parse_args()
    if args.cmd == "create-tenant":
        asyncio.run(create_tenant(args.name, args.rate, args.burst))


if __name__ == "__main__":
    main()
