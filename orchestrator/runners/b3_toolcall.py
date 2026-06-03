"""B3 - Tool/API call simulation (τ-bench-style).

Each "op" is one *trajectory* - a multi-step sequence of HTTP calls
against the b3-mock-api worker. Trajectories are sampled at random
from a pre-materialized pool built from 7 templates inspired by
tau-bench retail tasks (browse_only, buy_simple, buy_with_compare,
refund_flow, inventory_heavy, checkout_loop, profile_admin).

This is closer to real LLM tool-call traces than a single fixed
12-step script, and avoids artificially-favourable cache locality
in SQLite/pydantic.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time

import httpx

from config import Config
from metrics import LatencySink, ResourceSampler
from .b3_trajectories import build_trajectories
from .base import BenchmarkResult, Runner, cost_block, drive_load

log = logging.getLogger(__name__)


class B3Runner(Runner):
    name = "B3"
    workload = "toolcall"

    def __init__(self) -> None:
        self._trajectories, self._breakdown = build_trajectories(
            target_total=200, seed=42,
        )

    async def warmup(self, cfg: Config) -> None:
        async with httpx.AsyncClient(timeout=5.0) as c:
            for _ in range(30):
                try:
                    r = await c.get(f"{cfg.b3_api_url}/healthz")
                    if r.status_code == 200:
                        return
                except Exception:
                    pass
                await asyncio.sleep(1)
        raise RuntimeError("b3 mock-api did not become ready")

    async def run_one(self, cfg: Config, instance: dict, concurrency: int) -> BenchmarkResult:
        sink = LatencySink()
        per_call_sink = LatencySink()
        sampler = ResourceSampler()
        sampler.start()

        client = httpx.AsyncClient(
            base_url=cfg.b3_api_url,
            timeout=10.0,
            limits=httpx.Limits(
                max_connections=concurrency * 4,
                max_keepalive_connections=concurrency * 4,
            ),
        )

        # Each worker task picks a different per-task RNG seed so they
        # don't all walk the same trajectory at the same wall time.
        # All seeds are derived from a master seed for reproducibility.
        master_rng = random.Random(0xB3 ^ concurrency)
        seeds = [master_rng.randrange(1 << 30) for _ in range(concurrency)]
        seed_idx = {"i": 0}
        n = len(self._trajectories)
        total_steps = {"n": 0}

        async def one_trajectory() -> float:
            # First call inside a worker: take its dedicated rng seed.
            # Subsequent calls reuse it to walk a different trajectory
            # each iteration without colliding with peers.
            i = seed_idx["i"]
            seed_idx["i"] += 1
            rng = random.Random(seeds[i % concurrency] ^ i)
            traj = self._trajectories[rng.randrange(n)]

            t0 = time.perf_counter()
            for method, path, body in traj.steps:
                c0 = time.perf_counter()
                if method == "GET":
                    r = await client.get(path, params=body)
                else:
                    r = await client.post(path, json=body)
                r.raise_for_status()
                per_call_sink.record((time.perf_counter() - c0) * 1000.0)
            total_steps["n"] += len(traj.steps)
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
        calls_per_sec = total_steps["n"] / cfg.duration_sec
        avg_steps = (total_steps["n"] / ops) if ops else 0.0
        return BenchmarkResult(
            benchmark=self.name,
            instance=instance,
            concurrency=concurrency,
            duration_sec=cfg.duration_sec,
            throughput={
                "trajectories_per_sec": traj_per_sec,
                "tool_calls_per_sec": calls_per_sec,
                "total_trajectories": ops,
                "total_tool_calls": total_steps["n"],
            },
            latency_ms={
                "trajectory": sink.summary(),
                "per_call": per_call_sink.summary(),
            },
            resource=res,
            cost=cost_block(traj_per_sec, cfg.duration_sec, instance["instance_type"], cfg),
            extra={
                "trajectory_pool_size": n,
                "trajectory_breakdown": self._breakdown,
                "avg_steps_per_trajectory": round(avg_steps, 2),
            },
        )
