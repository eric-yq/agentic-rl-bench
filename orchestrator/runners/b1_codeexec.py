"""B1 - Python code execution.

The b1-codeexec-worker exposes POST /run {code, timeout}
returning {exit_code, stdout, stderr, wall_ms}.

We feed it a corpus of HumanEval + MBPP-sanitized canonical solutions
(plus their assert-style test cases). Each snippet is a self-contained
program that exits 0 on success, non-zero on assertion failure - which
mirrors how a real Agentic-RL sandbox verifies an LLM-generated patch.

Workload model is time-driven (run for `duration_sec` at the requested
concurrency, sample throughput / latency / resource), so corpus size
only affects coverage and P99 stability, not wall-clock duration.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time

import httpx

from config import Config
from metrics import LatencySink, ResourceSampler
from .b1_corpus import load_corpus
from .base import BenchmarkResult, Runner, cost_block, drive_load, schedule_sampler_reset

log = logging.getLogger(__name__)

# Per-snippet timeout (seconds). A handful of HumanEval tests touch
# brute-force / large-input edge cases, so 5s is too tight; 10s is
# conservative but still bounds long-tail.
SNIPPET_TIMEOUT = 10


class B1Runner(Runner):
    name = "B1"
    workload = "codeexec"

    def __init__(self) -> None:
        self._corpus, self._breakdown = load_corpus()
        # Shuffle once per process so different concurrency runs see the
        # same sequence (for comparability) but workers within a run
        # don't synchronize on the same problem at the same wall time.
        random.Random(42).shuffle(self._corpus)

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
        _reset_task = schedule_sampler_reset(sampler, cfg.warmup_sec, self.name)

        url = f"{cfg.b1_worker_url}/run"
        client = httpx.AsyncClient(
            timeout=SNIPPET_TIMEOUT * 3,
            limits=httpx.Limits(max_connections=concurrency * 2),
        )
        idx = {"i": 0}
        # exit_code != 0 means user code failed (assertion etc.). We still
        # count it as a successful sandbox roundtrip for throughput, but
        # track the rate so the report can flag if a workload regressed.
        passes = {"n": 0}
        n = len(self._corpus)

        async def one_call() -> float:
            i = idx["i"] % n
            idx["i"] += 1
            code = self._corpus[i]
            t0 = time.perf_counter()
            r = await client.post(
                url,
                json={"code": code, "timeout": SNIPPET_TIMEOUT},
            )
            r.raise_for_status()
            try:
                if r.json().get("exit_code") == 0:
                    passes["n"] += 1
            except Exception:
                pass
            return (time.perf_counter() - t0) * 1000.0

        try:
            ops = await drive_load(
                one_call,
                concurrency=concurrency,
                duration_sec=cfg.duration_sec,
                warmup_sec=cfg.warmup_sec,
                sink=sink,
            )
        finally:
            await client.aclose()
            res = await sampler.stop()

        tps = ops / cfg.duration_sec
        pass_rate = (passes["n"] / ops) if ops else 0.0
        return BenchmarkResult(
            benchmark=self.name,
            instance=instance,
            concurrency=concurrency,
            duration_sec=cfg.duration_sec,
            throughput={"executions_per_sec": tps, "total_ops": ops},
            latency_ms=sink.summary(),
            resource=res,
            cost=cost_block(tps, cfg.duration_sec, instance["instance_type"], cfg),
            extra={
                "corpus_size": n,
                "corpus_breakdown": self._breakdown,
                "snippet_timeout_sec": SNIPPET_TIMEOUT,
                "pass_rate": round(pass_rate, 4),
            },
        )
