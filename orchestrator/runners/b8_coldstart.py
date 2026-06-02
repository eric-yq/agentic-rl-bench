"""B8 - Container cold-start overhead.

Measures `docker run --rm <image> <cmd>` end-to-end latency.
Two scenarios:
  * minimal:  python:3.11-slim print(1)
  * realistic: python:3.11-slim with pip install requests then print(1)

Requires the docker socket mounted in to the orchestrator container.
"""

from __future__ import annotations

import asyncio
import logging
import time

from config import Config
from metrics import LatencySink, ResourceSampler
from .base import BenchmarkResult, Runner

log = logging.getLogger(__name__)


async def _run(cmd: list[str], timeout: float = 60.0) -> tuple[int, float]:
    t0 = time.perf_counter()
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        rc = await asyncio.wait_for(proc.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        rc = -1
    return rc, (time.perf_counter() - t0) * 1000.0


class B8Runner(Runner):
    name = "B8"
    workload = "coldstart"

    # B8 doesn't need warmup or compose-managed services
    async def warmup(self, cfg: Config) -> None:  # noqa: D401
        # Pre-pull the image so disk IO doesn't dominate first run
        rc, _ = await _run(["docker", "pull", cfg.b8_image], timeout=600)
        if rc != 0:
            raise RuntimeError(f"failed to pre-pull {cfg.b8_image}")

    async def run_one(self, cfg: Config, instance: dict, concurrency: int) -> BenchmarkResult:
        # Concurrency for B8 = number of parallel docker run calls.
        # Trials are split across `concurrency` workers.
        trials = cfg.b8_trials
        sink_minimal = LatencySink()
        sampler = ResourceSampler()
        sampler.start()

        cmd = [
            "docker", "run", "--rm",
            cfg.b8_image,
            "python", "-c", "print(1)",
        ]

        sem = asyncio.Semaphore(concurrency)
        completed = {"n": 0}

        async def worker():
            async with sem:
                rc, ms = await _run(cmd, timeout=120)
                if rc == 0:
                    sink_minimal.record(ms)
                    completed["n"] += 1
                else:
                    sink_minimal.fail()

        t0 = time.perf_counter()
        await asyncio.gather(*[worker() for _ in range(trials)])
        wall = time.perf_counter() - t0
        res = await sampler.stop()

        starts_per_sec = completed["n"] / wall if wall > 0 else 0
        return BenchmarkResult(
            benchmark=self.name,
            instance=instance,
            concurrency=concurrency,
            duration_sec=wall,
            throughput={
                "container_starts_per_sec": starts_per_sec,
                "total_starts": completed["n"],
            },
            latency_ms=sink_minimal.summary(),
            resource=res,
            cost={},  # cold-start cost is captured indirectly in B9
            extra={
                "trials": trials,
                "image": cfg.b8_image,
                "scenario": "minimal_python_print",
            },
        )
