"""Latency / throughput / resource sampling and aggregation.

ResourceSampler reports CPU utilisation from two viewpoints:
  - `cpu_util_avg`        : the host machine as a whole (read via
                            /proc/stat from inside the container, which
                            in Linux always reflects the host kernel's
                            aggregate CPU stats - so this number is what
                            you'd see in `dstat` or `top` on the host).
  - `orch_cpu_util_avg`   : the orchestrator process itself (mostly
                            asyncio event-loop saturation; useful to
                            tell if the orchestrator is the bottleneck).
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import psutil


@dataclass
class LatencySink:
    """Thread-safe-ish recorder for per-request latencies (ms)."""

    samples: list[float] = field(default_factory=list)
    errors: int = 0

    def record(self, latency_ms: float) -> None:
        self.samples.append(latency_ms)

    def fail(self) -> None:
        self.errors += 1

    def summary(self) -> dict:
        if not self.samples:
            return {
                "count": 0,
                "errors": self.errors,
                "p50": None, "p95": None, "p99": None,
                "mean": None, "max": None,
            }
        arr = np.array(self.samples)
        return {
            "count": int(arr.size),
            "errors": self.errors,
            "p50": float(np.percentile(arr, 50)),
            "p95": float(np.percentile(arr, 95)),
            "p99": float(np.percentile(arr, 99)),
            "mean": float(arr.mean()),
            "max": float(arr.max()),
        }


# /proc/stat is shared with the host kernel even inside containers
# (unless explicitly remounted). On hosts that bind-mount /proc/host
# we prefer that for unambiguous host-level CPU stats.
_PROC_STAT = "/proc/host/stat" if os.path.exists("/proc/host/stat") else "/proc/stat"
_NCPU = os.cpu_count() or 1


def _read_cpu_totals() -> tuple[int, int]:
    """Return (idle_jiffies, total_jiffies) from /proc/stat aggregate line."""
    with open(_PROC_STAT) as f:
        first = f.readline().split()
    # cpu  user nice system idle iowait irq softirq steal guest guest_nice
    vals = [int(x) for x in first[1:]]
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)  # idle + iowait
    total = sum(vals)
    return idle, total


class ResourceSampler:
    """Background task: sample CPU + memory at fixed interval."""

    def __init__(self, interval: float = 1.0) -> None:
        self.interval = interval
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self.cpu_host: list[float] = []      # host-wide %
        self.cpu_orch: list[float] = []      # this process %
        self.mem_gb: list[float] = []
        self.ctx_switches_per_s: list[float] = []
        self._last_ctx = psutil.cpu_stats().ctx_switches
        self._last_t = time.monotonic()
        self._last_idle, self._last_total = _read_cpu_totals()
        self._proc = psutil.Process()
        # Prime per-process counter: first call returns 0.0.
        self._proc.cpu_percent(interval=None)

    def _sample_host_cpu(self) -> float:
        idle, total = _read_cpu_totals()
        d_idle = idle - self._last_idle
        d_total = total - self._last_total
        self._last_idle, self._last_total = idle, total
        if d_total <= 0:
            return 0.0
        return 100.0 * (1.0 - d_idle / d_total)

    async def _loop(self) -> None:
        while not self._stop.is_set():
            self.cpu_host.append(self._sample_host_cpu())
            # `cpu_percent(None)` returns process CPU% normalised to 100%
            # per core; divide by ncpu to get fraction-of-machine.
            self.cpu_orch.append(
                self._proc.cpu_percent(interval=None) / max(_NCPU, 1)
            )
            mem = psutil.virtual_memory()
            self.mem_gb.append((mem.total - mem.available) / (1024**3))
            now = time.monotonic()
            ctx = psutil.cpu_stats().ctx_switches
            dt = max(now - self._last_t, 1e-6)
            self.ctx_switches_per_s.append((ctx - self._last_ctx) / dt)
            self._last_ctx, self._last_t = ctx, now
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                pass

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> dict:
        self._stop.set()
        if self._task:
            await self._task
        return {
            # Host-level CPU - use this to answer "is the machine busy?"
            "cpu_util_avg": float(np.mean(self.cpu_host)) if self.cpu_host else 0.0,
            "cpu_util_p95": float(np.percentile(self.cpu_host, 95)) if self.cpu_host else 0.0,
            # Orchestrator-process CPU - use this to spot client-side
            # bottlenecks. If this is near 100% but cpu_util_avg is low,
            # the load generator is the limiting factor, not the worker.
            "orch_cpu_util_avg": float(np.mean(self.cpu_orch)) if self.cpu_orch else 0.0,
            "orch_cpu_util_p95": float(np.percentile(self.cpu_orch, 95)) if self.cpu_orch else 0.0,
            "mem_peak_gb": float(np.max(self.mem_gb)) if self.mem_gb else 0.0,
            "ctx_switch_per_sec_avg": float(np.mean(self.ctx_switches_per_s)) if self.ctx_switches_per_s else 0.0,
            "host_ncpu": _NCPU,
        }
