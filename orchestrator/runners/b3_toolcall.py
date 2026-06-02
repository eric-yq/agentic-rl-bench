"""B3 - Tool/API call simulation.

Mock API exposes a small set of endpoints (search/order/refund/...).
Each "trajectory" is a fixed sequence of 12 calls.
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

ENDPOINTS = [
    ("GET",  "/search",         {"q": "headphones"}),
    ("GET",  "/product/42",     None),
    ("POST", "/cart/add",       {"sku": "SKU-42", "qty": 1}),
    ("GET",  "/cart",           None),
    ("POST", "/checkout",       {"payment": "card"}),
    ("GET",  "/order/last",     None),
    ("POST", "/order/refund",   {"reason": "test"}),
    ("GET",  "/profile",        None),
    ("POST", "/profile/update", {"name": "alice"}),
    ("GET",  "/recommend",      {"k": 5}),
    ("GET",  "/inventory",      {"sku": "SKU-42"}),
    ("GET",  "/healthz",        None),
]


class B3Runner(Runner):
    name = "B3"
    workload = "toolcall"

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

        async def one_trajectory() -> float:
            t0 = time.perf_counter()
            for method, path, body in ENDPOINTS:
                c0 = time.perf_counter()
                if method == "GET":
                    r = await client.get(path, params=body)
                else:
                    r = await client.post(path, json=body)
                r.raise_for_status()
                per_call_sink.record((time.perf_counter() - c0) * 1000.0)
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
        calls_per_sec = traj_per_sec * len(ENDPOINTS)
        return BenchmarkResult(
            benchmark=self.name,
            instance=instance,
            concurrency=concurrency,
            duration_sec=cfg.duration_sec,
            throughput={
                "trajectories_per_sec": traj_per_sec,
                "tool_calls_per_sec": calls_per_sec,
                "total_trajectories": ops,
            },
            latency_ms={
                "trajectory": sink.summary(),
                "per_call": per_call_sink.summary(),
            },
            resource=res,
            cost=cost_block(traj_per_sec, cfg.duration_sec, instance["instance_type"], cfg),
            extra={"steps_per_trajectory": len(ENDPOINTS)},
        )
