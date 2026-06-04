"""B9 - Concurrent end-to-end rollouts.

Simulates an RL trainer dispatching many rollouts in parallel to a
sandbox cluster. Each "rollout" is a mini-episode of 10..30 ops
where each op is sampled from a weighted task mix:

    B3 (tool-call trajectory)   weight 0.60
    B1 (python code execution)  weight 0.25
    B5 (DuckDB TPC-H query)     weight 0.15

A rollout completes when all its ops have returned successfully (or
one of them errored hard). Throughput is reported as
`rollouts_per_sec`; latency is the full rollout wall time.

The signal this captures - vs running B1/B3/B5 in isolation - is
**long-tail amplification**: one slow op in a rollout drags the whole
rollout's wall time, exactly like an RL synchronous batch waits for
the slowest sandbox.

Defaults reuse the time-driven `drive_load` model (run for
`b9_duration_sec` seconds at the requested concurrency).

Note: B9 reuses the b1-codeexec-worker, b3-mock-api and b5-sql-runner
services. `scripts/run-single.sh B9` brings all three up; the
orchestrator does not need to know anything new beyond their URLs.
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
from .b3_trajectories import build_trajectories as build_b3_trajectories
from .base import BenchmarkResult, Runner, cost_block, drive_load

log = logging.getLogger(__name__)

# Task mix - weights determine sampling probability per op.
TASK_MIX = [
    ("B3", 0.60),
    ("B1", 0.25),
    ("B5", 0.15),
]

# Episode length range (inclusive). Each rollout has a length
# uniformly sampled from this range, so concurrent rollouts have
# heterogeneous wall times - the RL-realistic case.
EPISODE_MIN_STEPS = 10
EPISODE_MAX_STEPS = 30

TPCH_QUERIES = list(range(1, 23))


class B9Runner(Runner):
    name = "B9"
    workload = "concurrent"

    def __init__(self) -> None:
        # Each downstream task picks its own input from a corpus / pool.
        # We don't shuffle them: the per-rollout RNG randomly indexes
        # into the pool every step, so input order doesn't matter.
        self._b1_corpus, _ = load_corpus()
        self._b3_trajectories, _ = build_b3_trajectories(target_total=200, seed=93)
        # Cache cumulative weights for fast random.choices-like sampling.
        names = [t for t, _ in TASK_MIX]
        weights = [w for _, w in TASK_MIX]
        cum = []
        s = 0.0
        for w in weights:
            s += w
            cum.append(s)
        self._task_names = names
        self._task_cum = cum
        self._task_total = s

    def _pick_task(self, rng: random.Random) -> str:
        x = rng.random() * self._task_total
        for name, c in zip(self._task_names, self._task_cum):
            if x < c:
                return name
        return self._task_names[-1]

    async def warmup(self, cfg: Config) -> None:
        # Verify all three downstream workers are healthy.
        urls = [
            ("b1", f"{cfg.b1_worker_url}/healthz"),
            ("b3", f"{cfg.b3_api_url}/healthz"),
            ("b5", f"{cfg.b5_worker_url}/healthz"),
        ]
        async with httpx.AsyncClient(timeout=10.0) as c:
            for name, url in urls:
                ok = False
                for _ in range(180):
                    try:
                        r = await c.get(url)
                        if r.status_code == 200:
                            data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
                            if data.get("ok", True):
                                ok = True
                                break
                    except Exception:
                        pass
                    await asyncio.sleep(1)
                if not ok:
                    raise RuntimeError(f"B9 dependency {name} not ready at {url}")

    async def run_one(
        self, cfg: Config, instance: dict, concurrency: int
    ) -> BenchmarkResult:
        sink = LatencySink()
        per_task_sink: dict[str, LatencySink] = {
            t: LatencySink() for t, _ in TASK_MIX
        }
        sampler = ResourceSampler()
        sampler.start()

        # B9 has its own duration knob (defaults to 1800s = 30min).
        duration = cfg.b9_duration_sec or cfg.duration_sec

        # One pooled HTTP client per downstream service.
        b1_client = httpx.AsyncClient(
            timeout=60.0,
            limits=httpx.Limits(max_connections=concurrency * 2),
        )
        b3_client = httpx.AsyncClient(
            base_url=cfg.b3_api_url,
            timeout=60.0,
            limits=httpx.Limits(max_connections=concurrency * 4),
        )
        b5_client = httpx.AsyncClient(
            timeout=600.0,
            limits=httpx.Limits(max_connections=concurrency * 2),
        )

        master_rng = random.Random(0xB9 ^ concurrency)
        seeds = [master_rng.randrange(1 << 30) for _ in range(concurrency)]
        seed_idx = {"i": 0}

        ops_b1 = {"n": 0}
        ops_b3 = {"n": 0}
        ops_b5 = {"n": 0}
        steps_total = {"n": 0}

        async def _do_b1(rng: random.Random) -> float:
            code = self._b1_corpus[rng.randrange(len(self._b1_corpus))]
            t0 = time.perf_counter()
            r = await b1_client.post(
                f"{cfg.b1_worker_url}/run",
                json={"code": code, "timeout": 10},
            )
            r.raise_for_status()
            ops_b1["n"] += 1
            return (time.perf_counter() - t0) * 1000.0

        async def _do_b3(rng: random.Random) -> float:
            traj = self._b3_trajectories[rng.randrange(len(self._b3_trajectories))]
            t0 = time.perf_counter()
            for method, path, body in traj.steps:
                if method == "GET":
                    r = await b3_client.get(path, params=body)
                else:
                    r = await b3_client.post(path, json=body)
                r.raise_for_status()
            ops_b3["n"] += 1
            return (time.perf_counter() - t0) * 1000.0

        async def _do_b5(rng: random.Random) -> float:
            q = TPCH_QUERIES[rng.randrange(len(TPCH_QUERIES))]
            t0 = time.perf_counter()
            r = await b5_client.post(
                f"{cfg.b5_worker_url}/query", json={"q": q}
            )
            r.raise_for_status()
            ops_b5["n"] += 1
            return (time.perf_counter() - t0) * 1000.0

        async def one_rollout() -> float:
            i = seed_idx["i"]
            seed_idx["i"] += 1
            rng = random.Random(seeds[i % concurrency] ^ i)
            n_steps = rng.randint(EPISODE_MIN_STEPS, EPISODE_MAX_STEPS)

            t0 = time.perf_counter()
            for _ in range(n_steps):
                task = self._pick_task(rng)
                if task == "B1":
                    lat = await _do_b1(rng)
                elif task == "B3":
                    lat = await _do_b3(rng)
                else:
                    lat = await _do_b5(rng)
                per_task_sink[task].record(lat)
            steps_total["n"] += n_steps
            return (time.perf_counter() - t0) * 1000.0

        try:
            ops = await drive_load(
                one_rollout,
                concurrency=concurrency,
                duration_sec=duration,
                sink=sink,
            )
        finally:
            await b1_client.aclose()
            await b3_client.aclose()
            await b5_client.aclose()
            res = await sampler.stop()

        rollouts_per_sec = ops / duration
        steps_per_sec = steps_total["n"] / duration
        avg_steps = (steps_total["n"] / ops) if ops else 0.0

        per_task_summary = {
            t: {
                "ops": (
                    ops_b1["n"] if t == "B1"
                    else ops_b3["n"] if t == "B3"
                    else ops_b5["n"]
                ),
                **per_task_sink[t].summary(),
            }
            for t, _ in TASK_MIX
        }

        return BenchmarkResult(
            benchmark=self.name,
            instance=instance,
            concurrency=concurrency,
            duration_sec=duration,
            throughput={
                "rollouts_per_sec": rollouts_per_sec,
                "steps_per_sec": steps_per_sec,
                "total_rollouts": ops,
                "total_steps": steps_total["n"],
            },
            latency_ms={
                "rollout": sink.summary(),
                "per_task": per_task_summary,
            },
            resource=res,
            cost=cost_block(rollouts_per_sec, duration, instance["instance_type"], cfg),
            extra={
                "task_mix": dict(TASK_MIX),
                "episode_steps_range": [EPISODE_MIN_STEPS, EPISODE_MAX_STEPS],
                "avg_steps_per_rollout": round(avg_steps, 2),
                "ops_breakdown": {
                    "B1": ops_b1["n"],
                    "B3": ops_b3["n"],
                    "B5": ops_b5["n"],
                },
            },
        )
