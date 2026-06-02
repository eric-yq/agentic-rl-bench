"""Idempotent seed for B5 postgres."""

import asyncio
import os
import sys

import asyncpg

DSN = os.getenv("PG_DSN", "postgresql://bench:bench@b5-postgres:5432/bench")


async def main() -> int:
    for attempt in range(60):
        try:
            conn = await asyncpg.connect(DSN)
            break
        except Exception as e:
            print(f"waiting for postgres ({attempt}): {e}", flush=True)
            await asyncio.sleep(1)
    else:
        return 1

    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS items (
            id SERIAL PRIMARY KEY,
            sku TEXT, name TEXT, price NUMERIC, qty INT
        );
        CREATE INDEX IF NOT EXISTS idx_items_sku ON items(sku);
        """
    )
    n = await conn.fetchval("SELECT COUNT(*) FROM items")
    if n < 100_000:
        print(f"seeding (current rows: {n}) ...", flush=True)
        await conn.execute(
            """
            INSERT INTO items (sku, name, price, qty)
            SELECT 'SKU-' || g, 'item-' || g, (random() * 100)::numeric(10,2),
                   (random()*1000)::int
            FROM generate_series(1, 100000) g
            """
        )
    print("done", flush=True)
    await conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
