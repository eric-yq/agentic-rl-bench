"""Multi-process load driver for HTTP-bound benchmarks.

A single asyncio event loop tops out around 50-100k req/s (with
uvloop) because all socket reads, JSON parsing and httpx coroutine
machinery is single-threaded. For B3/B7/B9 where each request is
sub-millisecond, that's the binding constraint - the orchestrator
maxes out one CPU long before the worker container does.

This module spawns N child processes (defaults to one per vCPU),
each running its own asyncio loop with `concurrency / N` workers.
Each child returns a partial result; the parent aggregates.

Usage from a runner:

    from .multiproc import drive_load_mp
    summary = await drive_load_mp(
        request_factory=_make_traj_request,
        concurrency=concurrency,
        duration_sec=cfg.duration_sec,
        nprocs=cfg.client_nprocs or auto,
    )
"""

from __future__ import annotations

import asyncio
import json
import logging
import multiprocessing as mp
import os
import time
from typing import Any, Awaitable, Callable

import numpy as np

log = logging.getLogger(__name__)


def _default_nprocs() -> int:
    """One client process per vCPU available to the container."""
    try:
        return max(1, len(os.sched_getaffinity(0)))
    except Exception:
        return os.cpu_count() or 1


# ---------------------------------------------------------------
# Child-process entry point
# ---------------------------------------------------------------

def _child_main(
    *,
    setup_pickled: bytes,
    work_factory_pickled: bytes,
    concurrency: int,
    duration_sec: float,
    seed_base: int,
    out_q: mp.Queue,
) -> None:
    """Run an asyncio event loop with `concurrency` workers for
    `duration_sec` seconds. Send the result dict back via `out_q`.
    """
    # Use uvloop in children too.
    try:
        import uvloop
        uvloop.install()
    except ImportError:
        pass

    import pickle
    setup = pickle.loads(setup_pickled)
    work_factory = pickle.loads(work_factory_pickled)

    async def _run() -> dict:
        # The factory builds an async work_fn that captures any HTTP
        # client / state needed; it can also return a teardown coroutine.
        work_fn, teardown = await work_factory(
            setup=setup, seed_base=seed_base, concurrency=concurrency,
        )
        latencies: list[float] = []
        completed = 0
        errors = 0
        end_at = time.monotonic() + duration_sec

        async def worker():
            nonlocal completed, errors
            while time.monotonic() < end_at:
                try:
                    lat = await work_fn()
                    if lat is not None:
                        latencies.append(lat)
                        completed += 1
                except Exception:
                    errors += 1

        tasks = [asyncio.create_task(worker()) for _ in range(concurrency)]
        await asyncio.gather(*tasks, return_exceptions=True)
        if teardown:
            try:
                await teardown()
            except Exception:
                pass

        return {
            "completed": completed,
            "errors": errors,
            "latencies_ms": latencies,
        }

    try:
        result = asyncio.run(_run())
        out_q.put(result)
    except Exception as e:
        out_q.put({"completed": 0, "errors": 0, "latencies_ms": [], "child_error": repr(e)})


# ---------------------------------------------------------------
# Parent-side helpers
# ---------------------------------------------------------------

async def drive_load_mp(
    *,
    work_factory: Callable[..., Awaitable],
    setup: Any,
    concurrency: int,
    duration_sec: float,
    nprocs: int | None = None,
    seed_base: int = 0,
) -> dict:
    """Spawn `nprocs` child processes, each driving `concurrency/nprocs`
    workers. Aggregate their results.

    `work_factory` is an async function `(setup, seed_base, concurrency) ->
    (work_fn, teardown)` where `work_fn()` returns a latency in ms per
    completed unit of work. It must be picklable (use module-level
    functions, not closures).

    Returns: dict with completed/errors/latencies_ms aggregated, plus
    per-child diagnostic info.
    """
    import pickle

    nprocs = nprocs or _default_nprocs()
    nprocs = max(1, min(nprocs, concurrency))

    # Distribute concurrency as evenly as possible.
    per_proc = [concurrency // nprocs] * nprocs
    for i in range(concurrency % nprocs):
        per_proc[i] += 1

    setup_blob = pickle.dumps(setup)
    factory_blob = pickle.dumps(work_factory)

    ctx = mp.get_context("spawn")
    out_q: mp.Queue = ctx.Queue()
    procs: list[mp.Process] = []
    for i, c in enumerate(per_proc):
        if c <= 0:
            continue
        p = ctx.Process(
            target=_child_main,
            kwargs=dict(
                setup_pickled=setup_blob,
                work_factory_pickled=factory_blob,
                concurrency=c,
                duration_sec=duration_sec,
                seed_base=seed_base + i * 1_000_003,
                out_q=out_q,
            ),
            daemon=True,
        )
        p.start()
        procs.append(p)

    # Drain the queue without blocking the asyncio loop.
    loop = asyncio.get_event_loop()
    results: list[dict] = []
    for _ in procs:
        res = await loop.run_in_executor(None, out_q.get)
        results.append(res)
    for p in procs:
        p.join(timeout=10)

    total_completed = sum(r.get("completed", 0) for r in results)
    total_errors = sum(r.get("errors", 0) for r in results)
    all_lat: list[float] = []
    for r in results:
        all_lat.extend(r.get("latencies_ms", []))

    if all_lat:
        arr = np.array(all_lat)
        latency_summary = {
            "count": int(arr.size),
            "errors": total_errors,
            "p50": float(np.percentile(arr, 50)),
            "p95": float(np.percentile(arr, 95)),
            "p99": float(np.percentile(arr, 99)),
            "mean": float(arr.mean()),
            "max": float(arr.max()),
        }
    else:
        latency_summary = {
            "count": 0, "errors": total_errors,
            "p50": None, "p95": None, "p99": None,
            "mean": None, "max": None,
        }

    return {
        "completed": total_completed,
        "errors": total_errors,
        "latency_ms": latency_summary,
        "nprocs": len(procs),
        "concurrency_per_proc": per_proc,
        "per_child": [
            {"completed": r.get("completed", 0),
             "errors": r.get("errors", 0),
             "child_error": r.get("child_error")}
            for r in results
        ],
    }
