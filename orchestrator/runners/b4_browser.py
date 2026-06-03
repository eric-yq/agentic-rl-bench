"""B4 - Browser automation via Playwright + headless Chromium.

Each "op" is one trajectory - a multi-step browser session against a
realistic static e-commerce SPA hosted by the b4-webarena-static
worker. Trajectories are sampled from a pool of 8 templates inspired
by WebArena shopping tasks.

The worker reports per-step success so we can compute a selector
miss rate. Latency is measured at the orchestrator (full HTTP RTT).
"""

from __future__ import annotations

import asyncio
import logging
import random
import time

import httpx

from config import Config
from metrics import LatencySink, ResourceSampler
from .b4_trajectories import build_trajectories
from .base import BenchmarkResult, Runner, cost_block, drive_load

log = logging.getLogger(__name__)


class B4Runner(Runner):
    name = "B4"
    workload = "browser"

    def __init__(self) -> None:
        self._trajectories, self._breakdown = build_trajectories(
            target_total=80, seed=42,
        )

    async def warmup(self, cfg: Config) -> None:
        async with httpx.AsyncClient(timeout=5.0) as c:
            for _ in range(60):
                try:
                    r = await c.get(f"{cfg.b4_worker_url}/healthz")
                    if r.status_code == 200 and r.json().get("ok"):
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
        # Browser context creation is heavy; allow generous per-task budget.
        client = httpx.AsyncClient(
            timeout=180.0,
            limits=httpx.Limits(max_connections=concurrency * 2),
        )

        master_rng = random.Random(0xB4 ^ concurrency)
        seeds = [master_rng.randrange(1 << 30) for _ in range(concurrency)]
        seed_idx = {"i": 0}
        n = len(self._trajectories)

        totals = {
            "actions": 0,
            "steps_done": 0,
            "steps_failed": 0,
        }

        async def one_trajectory() -> float:
            i = seed_idx["i"]
            seed_idx["i"] += 1
            rng = random.Random(seeds[i % concurrency] ^ i)
            traj = self._trajectories[rng.randrange(n)]

            payload = {
                "target_url": cfg.b4_target_url,
                "steps": [
                    {"action": s["action"], "args": s.get("args", {})}
                    for s in traj.steps
                ],
            }

            t0 = time.perf_counter()
            r = await client.post(url, json=payload)
            r.raise_for_status()
            data = r.json()
            totals["actions"]      += data.get("actions", 0)
            totals["steps_done"]   += data.get("steps_done", 0)
            totals["steps_failed"] += data.get("steps_failed", 0)
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

        traj_per_sec = ops / cfg.duration_sec
        actions_per_sec = totals["actions"] / cfg.duration_sec
        miss_rate = (
            totals["steps_failed"] / totals["actions"]
            if totals["actions"] else 0.0
        )

        return BenchmarkResult(
            benchmark=self.name,
            instance=instance,
            concurrency=concurrency,
            duration_sec=cfg.duration_sec,
            throughput={
                "trajectories_per_sec": traj_per_sec,
                "actions_per_sec": actions_per_sec,
                "total_trajectories": ops,
                "total_actions": totals["actions"],
            },
            latency_ms=sink.summary(),
            resource=res,
            cost=cost_block(traj_per_sec, cfg.duration_sec, instance["instance_type"], cfg),
            extra={
                "trajectory_pool_size": n,
                "trajectory_breakdown": self._breakdown,
                "selector_miss_rate": round(miss_rate, 4),
                "steps_done_total": totals["steps_done"],
                "steps_failed_total": totals["steps_failed"],
            },
        )
