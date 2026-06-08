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
    """Best-effort lookup of $/h for cost-per-rollout calculation.

    We anchor pricing on the .4xlarge SKUs (which the user configures
    in .env) and scale linearly by vCPU for larger sizes. AWS On-Demand
    pricing is in fact perfectly linear within a family (e.g.
    c8g.16xlarge = 4 x c8g.4xlarge), so this is exact for every standard
    size: large / xlarge / 2xlarge / 4xlarge / 8xlarge / 12xlarge /
    16xlarge / 24xlarge / 48xlarge.
    """
    t = instance_type.lower()
    # vCPU implied by instance size suffix (AWS standard).
    size_to_vcpus = {
        "large":     2,  "xlarge":     4,
        "2xlarge":   8,  "4xlarge":   16,  "8xlarge":   32,
        "12xlarge": 48,  "16xlarge":  64,  "24xlarge":  96,
        "48xlarge": 192,
    }
    # parse "<family>.<size>" e.g. "c8g.24xlarge"
    parts = t.split(".")
    if len(parts) < 2:
        return 0.0
    family, size = parts[0], parts[1]
    vcpus = size_to_vcpus.get(size)
    if vcpus is None:
        return 0.0
    # Pick the per-vCPU baseline price.
    base_4xl = None
    if family.startswith("c7i") or family.startswith("m7i"):
        base_4xl = cfg.price_c7i_4xl
    elif family.startswith("c8g") or family.startswith("m8g") or family.startswith("m9g"):
        base_4xl = cfg.price_c8g_4xl
    elif family.startswith("m8i"):
        # m8i uses the same Intel pricing curve as c7i/m7i within ~5%
        base_4xl = cfg.price_c7i_4xl
    if base_4xl is None:
        return 0.0
    # Linear scale: 4xlarge has 16 vCPUs.
    return base_4xl * (vcpus / 16.0)


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


def schedule_sampler_reset(
    sampler, warmup_sec: float, name: str = "?",
) -> "asyncio.Task | None":
    """Reset `sampler` after `warmup_sec` seconds so its averages reflect
    the steady-state phase only. No-op when warmup_sec <= 0.

    Returns the scheduled Task (caller should keep a reference so the
    task isn't garbage-collected before it fires) or None.
    """
    if warmup_sec <= 0:
        return None

    async def _reset():
        await asyncio.sleep(warmup_sec)
        sampler.reset()
        log.info("[%s] warmup window done; resource sampler reset", name)

    return asyncio.create_task(_reset())


# -----------------------------------------------------------------
# Generic load-driver: spawn N worker tasks for a fixed duration
# -----------------------------------------------------------------
async def drive_load(
    work_fn,
    *,
    concurrency: int,
    duration_sec: float,
    sink: LatencySink,
    warmup_sec: float = 0.0,
) -> int:
    """Run `work_fn()` repeatedly under `concurrency` workers.

    If `warmup_sec > 0`, run that long first WITHOUT counting any
    completions or recording latencies - this lets connection pools,
    interpreter caches and host TCP windows reach steady state before
    we start measuring.

    `work_fn` should be an async callable returning latency in ms,
    or raising on error. Returns total successful operations during
    the measurement window only.
    """
    measure_start = time.monotonic() + warmup_sec
    end_at = measure_start + duration_sec
    # asyncio is single-threaded; no lock needed for the counter, and
    # adding `async with lock` here measurably hurts throughput because
    # each += pays an extra event-loop round-trip.
    completed = 0

    async def worker():
        nonlocal completed
        while time.monotonic() < end_at:
            try:
                lat = await work_fn()
                if time.monotonic() < measure_start:
                    # Still warming up: discard.
                    continue
                if lat is not None:
                    sink.record(lat)
                    completed += 1
            except Exception as e:  # noqa: BLE001
                if time.monotonic() < measure_start:
                    continue
                sink.fail()
                log.debug("worker error: %s", e)

    tasks = [asyncio.create_task(worker()) for _ in range(concurrency)]
    await asyncio.gather(*tasks, return_exceptions=True)
    return completed
