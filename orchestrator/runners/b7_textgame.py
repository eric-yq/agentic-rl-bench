"""B7 - text-game simulation (ALFWorld / TextWorld-style).

Each "op" is one episode of `steps` actions executed inside the
b7-textgame worker. The simulator is a hand-rolled minigrid world
(8 rooms, 30 items, 6 goal templates) - the original B7 design
explicitly says we measure "scheduling + GIL + tight Python loops",
not ALFWorld's task-completion accuracy, so a pure-Python equivalent
gives the same signal without the heavy native deps.

Episode length sampled per request from [EPISODE_MIN, EPISODE_MAX]
to mirror the design doc's "30-50 steps each".
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

EPISODE_MIN = 30
EPISODE_MAX = 50
# Episodes per HTTP request. Sub-millisecond per-episode work would
# otherwise be drowned out by HTTP / FastAPI dispatch noise; batching
# brings each call into the few-ms range where the GIL + Python loop
# signal dominates - matching the original B7 design intent.
EPISODES_PER_REQUEST = 10


class B7Runner(Runner):
    name = "B7"
    workload = "textgame"

    async def warmup(self, cfg: Config) -> None:
        url = f"{cfg.b7_worker_url}/healthz"
        async with httpx.AsyncClient(timeout=5.0) as c:
            for _ in range(60):
                try:
                    r = await c.get(url)
                    if r.status_code == 200 and r.json().get("ok"):
                        return
                except Exception:
                    pass
                await asyncio.sleep(1)
        raise RuntimeError("b7 textgame worker did not become ready")

    async def run_one(
        self, cfg: Config, instance: dict, concurrency: int
    ) -> BenchmarkResult:
        sink = LatencySink()
        sampler = ResourceSampler()
        sampler.start()

        url = f"{cfg.b7_worker_url}/episode"
        client = httpx.AsyncClient(
            timeout=30.0,
            limits=httpx.Limits(max_connections=concurrency * 2),
        )

        master_rng = random.Random(0xB7 ^ concurrency)
        seeds = [master_rng.randrange(1 << 30) for _ in range(concurrency)]
        seed_idx = {"i": 0}
        steps_total = {"n": 0}
        reward_total = {"r": 0.0}

        async def one_episode() -> float:
            i = seed_idx["i"]
            seed_idx["i"] += 1
            rng = random.Random(seeds[i % concurrency] ^ i)
            steps = rng.randint(EPISODE_MIN, EPISODE_MAX)

            t0 = time.perf_counter()
            r = await client.post(url, json={
                "seed": rng.randrange(1 << 30),
                "steps": steps,
                "batch": EPISODES_PER_REQUEST,
            })
            r.raise_for_status()
            data = r.json()
            steps_total["n"] += data.get("steps_done", 0)
            reward_total["r"] += data.get("total_reward", 0.0)
            return (time.perf_counter() - t0) * 1000.0

        try:
            ops = await drive_load(
                one_episode,
                concurrency=concurrency,
                duration_sec=cfg.duration_sec,
                sink=sink,
            )
        finally:
            await client.aclose()
            res = await sampler.stop()

        # `ops` counted by drive_load is the number of HTTP requests we
        # made; each request bundles `EPISODES_PER_REQUEST` episodes.
        episodes_done = ops * EPISODES_PER_REQUEST
        episodes_per_sec = episodes_done / cfg.duration_sec
        steps_per_sec = steps_total["n"] / cfg.duration_sec
        avg_steps = (steps_total["n"] / episodes_done) if episodes_done else 0.0
        avg_reward = (reward_total["r"] / episodes_done) if episodes_done else 0.0
        return BenchmarkResult(
            benchmark=self.name,
            instance=instance,
            concurrency=concurrency,
            duration_sec=cfg.duration_sec,
            throughput={
                "episodes_per_sec": episodes_per_sec,
                "steps_per_sec": steps_per_sec,
                "requests_per_sec": ops / cfg.duration_sec,
                "total_episodes": episodes_done,
                "total_steps": steps_total["n"],
            },
            latency_ms=sink.summary(),
            resource=res,
            cost=cost_block(episodes_per_sec, cfg.duration_sec, instance["instance_type"], cfg),
            extra={
                "episode_steps_range": [EPISODE_MIN, EPISODE_MAX],
                "episodes_per_request": EPISODES_PER_REQUEST,
                "avg_steps_per_episode": round(avg_steps, 2),
                "avg_reward_per_episode": round(avg_reward, 4),
                "world_rooms": 8,
                "world_items": 30,
                "goal_templates": 6,
            },
        )
