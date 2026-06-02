"""B4 - Headless Chromium via Playwright worker.

The worker exposes POST /task {steps:[{action,args}], target_url}
returning {wall_ms, steps_done}.
A "trajectory" performs goto + several click/scroll/screenshot ops.
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

# Predefined trajectory: realistic shopping-arena pattern
TRAJECTORY = [
    {"action": "goto", "args": {"path": "/"}},
    {"action": "click", "args": {"selector": "a.product-link"}},
    {"action": "scroll", "args": {"y": 600}},
    {"action": "screenshot", "args": {}},
    {"action": "click", "args": {"selector": "button.add-to-cart"}},
    {"action": "goto", "args": {"path": "/cart"}},
    {"action": "screenshot", "args": {}},
]


class B4Runner(Runner):
    name = "B4"
    workload = "browser"

    async def warmup(self, cfg: Config) -> None:
        async with httpx.AsyncClient(timeout=5.0) as c:
            for _ in range(60):
                try:
                    r = await c.get(f"{cfg.b4_worker_url}/healthz")
                    if r.status_code == 200:
                        return
                except Exception:
                    pass
                await asyncio.sleep(1)
        raise RuntimeError("b4 playwright worker did not become ready")

    async def run_one(self, cfg: Config, instance: dict, concurrency: int) -> BenchmarkResult:
        sink = LatencySink()
        sampler = ResourceSampler()
        sampler.start()

        url = f"{cfg.b4_worker_url}/task"
        # Each trajectory holds a browser context, expensive: shorter timeouts
        client = httpx.AsyncClient(
            timeout=120.0,
            limits=httpx.Limits(max_connections=concurrency * 2),
        )

        payload = {"steps": TRAJECTORY, "target_url": cfg.b4_target_url}

        async def one_trajectory() -> float:
            t0 = time.perf_counter()
            r = await client.post(url, json=payload)
            r.raise_for_status()
            return (time.perf_counter() - t0) * 1000.0

        try:
            ops = await drive_load(
                one_trajectory,
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
            throughput={"trajectories_per_sec": tps, "total_trajectories": ops},
            latency_ms=sink.summary(),
            resource=res,
            cost=cost_block(tps, cfg.duration_sec, instance["instance_type"], cfg),
            extra={"steps_per_trajectory": len(TRAJECTORY)},
        )
