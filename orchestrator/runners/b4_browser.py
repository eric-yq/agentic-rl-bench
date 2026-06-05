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
from .base import BenchmarkResult, Runner, cost_block, drive_load, schedule_sampler_reset

log = logging.getLogger(__name__)


class B4Runner(Runner):
    name = "B4"
    workload = "browser"

    def __init__(self) -> None:
        self._trajectories, self._breakdown = build_trajectories(
            target_total=80, seed=42,
        )
        # Populated by warmup() via /ports discovery.
        self._worker_ports: list[int] = []

    async def warmup(self, cfg: Config) -> None:
        # Pool pre-create can take 10-30s on first start: WORKERS *
        # MAX_CONTEXTS chromium contexts spin up sequentially in each
        # worker. Be patient.
        url = f"{cfg.b4_worker_url}/healthz"
        last = "no response"
        async with httpx.AsyncClient(timeout=5.0) as c:
            for _ in range(180):
                try:
                    r = await c.get(url)
                    last = f"HTTP {r.status_code}: {r.text[:200]}"
                    if r.status_code == 200 and r.json().get("ok"):
                        # Discover the full set of worker ports for
                        # client-side round-robin (bypasses unbalanced
                        # kernel SO_REUSEPORT hashing).
                        try:
                            pr = await c.get(f"{cfg.b4_worker_url}/ports")
                            self._worker_ports = pr.json().get("ports", [])
                            log.info("[B4] discovered worker ports: %s",
                                     self._worker_ports)
                        except Exception:
                            self._worker_ports = []
                        return
                except Exception as e:
                    last = f"connect error: {e!r}"
                await asyncio.sleep(1)
        raise RuntimeError(
            f"b4 playwright worker did not become ready after 180s; "
            f"last response: {last}"
        )

    async def run_one(self, cfg: Config, instance: dict, concurrency: int) -> BenchmarkResult:
        sink = LatencySink()
        sampler = ResourceSampler()
        sampler.start()
        _reset_task = schedule_sampler_reset(sampler, cfg.warmup_sec, self.name)

        url = f"{cfg.b4_worker_url}/task"
        # Build the per-port URL list for client-side round-robin.
        # If discovery failed (legacy worker), fall back to the
        # single-URL behaviour (subject to kernel SO_REUSEPORT).
        if self._worker_ports:
            host = cfg.b4_worker_url.split("://", 1)[-1].split(":", 1)[0]
            scheme = "http"
            urls = [f"{scheme}://{host}:{p}/task" for p in self._worker_ports]
        else:
            urls = [url]
        log.info("[B4] dispatching to %d worker URL(s)", len(urls))

        # Browser context creation is heavy; allow generous per-task budget.
        #
        # Disable HTTP keep-alive so each request opens a fresh TCP
        # connection. Combined with explicit per-port round-robin
        # below, this guarantees even fanout across all worker
        # processes - bypassing the kernel's SO_REUSEPORT hash which
        # we measured as severely unbalanced on aarch64 (one PID at
        # 35% of requests, another at 1%).
        client = httpx.AsyncClient(
            timeout=180.0,
            limits=httpx.Limits(
                max_connections=concurrency * 2,
                max_keepalive_connections=0,
            ),
            headers={"Connection": "close"},
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

            # Strict round-robin across worker ports - guarantees
            # exactly equal load across Chromium masters regardless
            # of kernel scheduling behaviour.
            target_url = urls[i % len(urls)]
            t0 = time.perf_counter()
            r = await client.post(target_url, json=payload)
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
                warmup_sec=cfg.warmup_sec,
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
