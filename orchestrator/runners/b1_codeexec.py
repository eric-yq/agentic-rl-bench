"""B1 - Python code execution.

The b1-codeexec-worker exposes POST /run {code, timeout}
returning {exit_code, stdout, stderr, wall_ms}.
We submit a corpus of HumanEval-style snippets in random order.
"""

from __future__ import annotations

import asyncio
import logging
import time

import httpx

from config import Config
from metrics import LatencySink, ResourceSampler
from .base import BenchmarkResult, Runner, cost_block, drive_load

log = logging.getLogger(__name__)

# A small built-in corpus; in production load HumanEval / MBPP from disk.
CORPUS = [
    "print(sum(i*i for i in range(1000)))",
    "import math; print(math.factorial(15))",
    "s='abracadabra'; print(s[::-1])",
    "print([x for x in range(50) if x%7==0])",
    "import json; print(json.dumps({'a':1,'b':[1,2,3]}))",
    "def fib(n):\n a,b=0,1\n for _ in range(n): a,b=b,a+b\n return a\nprint(fib(25))",
    "import re; print(len(re.findall(r'\\\\w+', 'the quick brown fox')))",
    "print(sorted([3,1,4,1,5,9,2,6,5,3,5]))",
]


class B1Runner(Runner):
    name = "B1"
    workload = "codeexec"

    async def warmup(self, cfg: Config) -> None:
        url = f"{cfg.b1_worker_url}/healthz"
        async with httpx.AsyncClient(timeout=5.0) as c:
            for _ in range(30):
                try:
                    r = await c.get(url)
                    if r.status_code == 200:
                        return
                except Exception:
                    pass
                await asyncio.sleep(1)
        raise RuntimeError("b1 worker did not become ready")

    async def run_one(self, cfg: Config, instance: dict, concurrency: int) -> BenchmarkResult:
        sink = LatencySink()
        sampler = ResourceSampler()
        sampler.start()

        url = f"{cfg.b1_worker_url}/run"
        client = httpx.AsyncClient(timeout=30.0, limits=httpx.Limits(max_connections=concurrency * 2))
        idx = {"i": 0}

        async def one_call() -> float:
            code = CORPUS[idx["i"] % len(CORPUS)]
            idx["i"] += 1
            t0 = time.perf_counter()
            r = await client.post(url, json={"code": code, "timeout": 5})
            r.raise_for_status()
            return (time.perf_counter() - t0) * 1000.0

        try:
            ops = await drive_load(
                one_call,
                concurrency=concurrency,
                duration_sec=cfg.duration_sec,
                sink=sink,
            )
        finally:
            await client.aclose()
            res = await sampler.stop()

        tps = ops / cfg.duration_sec
        return BenchmarkResult(
            benchmark=self.name,
            instance=instance,
            concurrency=concurrency,
            duration_sec=cfg.duration_sec,
            throughput={"executions_per_sec": tps, "total_ops": ops},
            latency_ms=sink.summary(),
            resource=res,
            cost=cost_block(tps, cfg.duration_sec, instance["instance_type"], cfg),
            extra={"corpus_size": len(CORPUS)},
        )
