"""B5 - SQL execution against PostgreSQL (multi-arch official image).

Uses asyncpg directly so we measure DB CPU + connection round-trip,
not a separate HTTP hop. Schema + seed are loaded on first call.
"""

from __future__ import annotations

import asyncio
import logging
import time

import asyncpg

from config import Config
from metrics import LatencySink, ResourceSampler
from .base import BenchmarkResult, Runner, cost_block, drive_load

log = logging.getLogger(__name__)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS items (
    id SERIAL PRIMARY KEY,
    sku TEXT, name TEXT, price NUMERIC, qty INT
);
CREATE INDEX IF NOT EXISTS idx_items_sku ON items(sku);
"""
SEED_SQL = """
INSERT INTO items (sku, name, price, qty)
SELECT 'SKU-' || g, 'item-' || g, (random() * 100)::numeric(10,2), (random()*1000)::int
FROM generate_series(1, 100000) g
ON CONFLICT DO NOTHING;
"""

QUERIES = [
    ("SELECT COUNT(*) FROM items", None),
    ("SELECT * FROM items WHERE sku = $1", ("SKU-1234",)),
    ("SELECT AVG(price) FROM items WHERE qty > $1", (500,)),
    ("SELECT sku, name, price FROM items ORDER BY price DESC LIMIT 10", None),
    ("SELECT COUNT(DISTINCT sku) FROM items WHERE price BETWEEN $1 AND $2", (10, 50)),
]


class B5Runner(Runner):
    name = "B5"
    workload = "sqlexec"

    async def warmup(self, cfg: Config) -> None:
        for _ in range(60):
            try:
                conn = await asyncpg.connect(cfg.b5_pg_dsn)
                await conn.execute(SCHEMA_SQL)
                count = await conn.fetchval("SELECT COUNT(*) FROM items")
                if count < 100000:
                    log.info("seeding b5 items table ...")
                    await conn.execute(SEED_SQL)
                await conn.close()
                return
            except Exception as e:
                log.debug("waiting for postgres: %s", e)
                await asyncio.sleep(1)
        raise RuntimeError("b5 postgres did not become ready")

    async def run_one(self, cfg: Config, instance: dict, concurrency: int) -> BenchmarkResult:
        sink = LatencySink()
        sampler = ResourceSampler()
        sampler.start()

        pool = await asyncpg.create_pool(
            cfg.b5_pg_dsn,
            min_size=concurrency,
            max_size=concurrency * 2,
            command_timeout=30,
        )
        idx = {"i": 0}

        async def one_query() -> float:
            sql, args = QUERIES[idx["i"] % len(QUERIES)]
            idx["i"] += 1
            t0 = time.perf_counter()
            async with pool.acquire() as conn:
                if args:
                    await conn.fetch(sql, *args)
                else:
                    await conn.fetch(sql)
            return (time.perf_counter() - t0) * 1000.0

        try:
            ops = await drive_load(
                one_query,
                concurrency=concurrency,
                duration_sec=cfg.duration_sec,
                sink=sink,
            )
        finally:
            await pool.close()
            res = await sampler.stop()

        qps = ops / cfg.duration_sec
        return BenchmarkResult(
            benchmark=self.name,
            instance=instance,
            concurrency=concurrency,
            duration_sec=cfg.duration_sec,
            throughput={"queries_per_sec": qps, "total_queries": ops},
            latency_ms=sink.summary(),
            resource=res,
            cost=cost_block(qps, cfg.duration_sec, instance["instance_type"], cfg),
            extra={"query_mix_size": len(QUERIES)},
        )
