"""B5 - SQL execution against DuckDB in-process (TPC-H workload).

The b5-sql-runner worker hosts DuckDB with the official `tpch`
extension and 22 generated tables (sf controlled by TPCH_SF env on
the worker container). We submit POST /query {q: 1..22} and time
the round-trip, plus per-query worker-side wall_ms.

Q-mix: time-driven cycling through Q1..Q22 with a deterministic
shuffle. A few queries (Q11, Q15, Q17, Q20) are notably heavier
than the rest, so cycling guarantees a balanced distribution.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time

import httpx

from config import Config
from metrics import LatencySink, ResourceSampler
from .base import BenchmarkResult, Runner, cost_block, drive_load

log = logging.getLogger(__name__)

# All 22 official TPC-H queries.
TPCH_QUERIES = list(range(1, 23))


class B5Runner(Runner):
    name = "B5"
    workload = "sqlexec"

    def __init__(self) -> None:
        # Deterministic shuffle so different concurrency runs see the
        # same query stream and results are comparable.
        self._mix = TPCH_QUERIES.copy()
        random.Random(42).shuffle(self._mix)

    async def warmup(self, cfg: Config) -> None:
        # dbgen for SF=1 takes ~30s on a 4xlarge. Be generous.
        url = f"{cfg.b5_worker_url}/healthz"
        last_status: str = "no response yet"
        async with httpx.AsyncClient(timeout=10.0) as c:
            for _ in range(180):
                try:
                    r = await c.get(url)
                    last_status = f"HTTP {r.status_code}: {r.text[:200]}"
                    if r.status_code == 200 and r.json().get("ok"):
                        info = await c.get(f"{cfg.b5_worker_url}/info")
                        if info.status_code == 200:
                            log.info("b5 ready: %s", info.json())
                        return
                except Exception as e:
                    last_status = f"connect error: {e!r}"
                await asyncio.sleep(1)
        raise RuntimeError(
            f"b5 sql-runner did not become ready after 180s; "
            f"last response: {last_status}. "
            f"Tip: `docker logs arl-b5-runner` to see worker startup; "
            f"common cause is `INSTALL tpch` blocked by network policy. "
            f"Rebuilding the image bakes the extension at build time."
        )

    async def run_one(self, cfg: Config, instance: dict, concurrency: int) -> BenchmarkResult:
        sink = LatencySink()
        per_q_sink: dict[int, LatencySink] = {q: LatencySink() for q in TPCH_QUERIES}
        sampler = ResourceSampler()
        sampler.start()

        url = f"{cfg.b5_worker_url}/query"
        client = httpx.AsyncClient(
            timeout=600.0,  # heavier queries can take many seconds at high concurrency
            limits=httpx.Limits(max_connections=concurrency * 2),
        )

        idx = {"i": 0}
        per_q_count: dict[int, int] = {q: 0 for q in TPCH_QUERIES}

        async def one_query() -> float:
            i = idx["i"]
            idx["i"] += 1
            q = self._mix[i % len(self._mix)]
            t0 = time.perf_counter()
            r = await client.post(url, json={"q": q})
            r.raise_for_status()
            wall = (time.perf_counter() - t0) * 1000.0
            per_q_sink[q].record(wall)
            per_q_count[q] += 1
            return wall

        try:
            ops = await drive_load(
                one_query,
                concurrency=concurrency,
                duration_sec=cfg.duration_sec,
                sink=sink,
            )
        finally:
            await client.aclose()
            res = await sampler.stop()

        # Pull per-query latency summaries and counts.
        per_query: dict[str, dict] = {}
        for q in TPCH_QUERIES:
            n = per_q_count[q]
            if n == 0:
                continue
            per_query[f"Q{q:02d}"] = {
                "count": n,
                **per_q_sink[q].summary(),
            }

        # Fetch worker-side info (sf, threads, dbgen time, row counts)
        # for the result blob - useful for reproducibility.
        worker_info: dict = {}
        try:
            async with httpx.AsyncClient(timeout=5.0) as c:
                r = await c.get(f"{cfg.b5_worker_url}/info")
                if r.status_code == 200:
                    worker_info = r.json()
        except Exception:
            pass

        qps = ops / cfg.duration_sec
        return BenchmarkResult(
            benchmark=self.name,
            instance=instance,
            concurrency=concurrency,
            duration_sec=cfg.duration_sec,
            throughput={"queries_per_sec": qps, "total_queries": ops},
            latency_ms={
                "trajectory": sink.summary(),  # one query == one op
                "per_query": per_query,
            },
            resource=res,
            cost=cost_block(qps, cfg.duration_sec, instance["instance_type"], cfg),
            extra={
                "engine": "duckdb",
                "tpch_sf": worker_info.get("tpch_sf"),
                "duckdb_version": worker_info.get("duckdb_version"),
                "duckdb_threads": worker_info.get("duckdb_threads"),
                "dbgen_sec": worker_info.get("dbgen_sec"),
                "row_counts": worker_info.get("row_counts"),
                "query_pool": TPCH_QUERIES,
            },
        )
