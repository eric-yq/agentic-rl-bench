"""Base classes for benchmark runners."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from config import Config
from metrics import LatencySink, ResourceSampler

log = logging.getLogger(__name__)


@dataclass
class BenchmarkResult:
    benchmark: str
    instance: dict[str, Any]
    concurrency: int
    duration_sec: float
    throughput: dict[str, float] = field(default_factory=dict)
    latency_ms: dict[str, Any] = field(default_factory=dict)
    resource: dict[str, Any] = field(default_factory=dict)
    cost: dict[str, Any] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: time.strftime("%Y%m%d-%H%M%S"))

    def to_dict(self) -> dict:
        return {
            "benchmark": self.benchmark,
            "instance": self.instance,
            "concurrency": self.concurrency,
            "duration_sec": self.duration_sec,
            "throughput": self.throughput,
            "latency_ms": self.latency_ms,
            "resource": self.resource,
            "cost": self.cost,
            "extra": self.extra,
            "timestamp": self.timestamp,
        }


def hourly_price(instance_type: str, cfg: Config) -> float:
    """Best-effort lookup of $/h for cost-per-rollout calculation."""
    t = instance_type.lower()
    if t.startswith("c7i.4xlarge"):
        return cfg.price_c7i_4xl
    if t.startswith("c8g.4xlarge"):
        return cfg.price_c8g_4xl
    # Linear-by-vCPU fallback for other sizes (very rough)
    if t.startswith("c7i."):
        return cfg.price_c7i_4xl  # caller can override
    if t.startswith("c8g."):
        return cfg.price_c8g_4xl
    return 0.0


def cost_block(throughput_per_sec: float, duration: float, instance_type: str, cfg: Config) -> dict:
    price_h = hourly_price(instance_type, cfg)
    total_units = max(throughput_per_sec * duration, 1e-9)
    cost_run = price_h * (duration / 3600.0)
    return {
        "instance_hourly_usd": price_h,
        "cost_per_1k_units_usd": (cost_run / total_units) * 1000.0 if price_h > 0 else None,
    }


class Runner:
    """Subclasses implement `run_one(cfg, instance, concurrency)`.

    The base orchestrator will call this once per concurrency level.
    """

    name: str = "BASE"
    workload: str = "base"

    async def warmup(self, cfg: Config) -> None:  # noqa: D401
        """Override to perform service-readiness checks before measurement."""
        await asyncio.sleep(1)

    async def run_one(
        self, cfg: Config, instance: dict, concurrency: int
    ) -> BenchmarkResult:
        raise NotImplementedError


# -----------------------------------------------------------------
# Generic load-driver: spawn N worker tasks for a fixed duration
# -----------------------------------------------------------------
async def drive_load(
    work_fn,
    *,
    concurrency: int,
    duration_sec: float,
    sink: LatencySink,
) -> int:
    """Run `work_fn()` repeatedly under `concurrency` workers for `duration_sec`.

    `work_fn` should be an async callable returning latency in ms,
    or raising on error. Returns total successful operations.
    """
    end_at = time.monotonic() + duration_sec
    # asyncio is single-threaded; no lock needed for the counter, and
    # adding `async with lock` here measurably hurts throughput because
    # each += pays an extra event-loop round-trip.
    completed = 0

    async def worker():
        nonlocal completed
        while time.monotonic() < end_at:
            try:
                lat = await work_fn()
                if lat is not None:
                    sink.record(lat)
                    completed += 1
            except Exception as e:  # noqa: BLE001
                sink.fail()
                log.debug("worker error: %s", e)

    tasks = [asyncio.create_task(worker()) for _ in range(concurrency)]
    await asyncio.gather(*tasks, return_exceptions=True)
    return completed
