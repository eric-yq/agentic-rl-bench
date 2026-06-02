"""Latency / throughput / resource sampling and aggregation."""

from __future__ import annotations

import asyncio
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


class ResourceSampler:
    """Background task: sample host CPU + memory at fixed interval."""

    def __init__(self, interval: float = 1.0) -> None:
        self.interval = interval
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self.cpu: list[float] = []
        self.mem_gb: list[float] = []
        self.ctx_switches_per_s: list[float] = []
        self._last_ctx = psutil.cpu_stats().ctx_switches
        self._last_t = time.monotonic()

    async def _loop(self) -> None:
        while not self._stop.is_set():
            self.cpu.append(psutil.cpu_percent(interval=None))
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
            "cpu_util_avg": float(np.mean(self.cpu)) if self.cpu else 0.0,
            "cpu_util_p95": float(np.percentile(self.cpu, 95)) if self.cpu else 0.0,
            "mem_peak_gb": float(np.max(self.mem_gb)) if self.mem_gb else 0.0,
            "ctx_switch_per_sec_avg": float(np.mean(self.ctx_switches_per_s)) if self.ctx_switches_per_s else 0.0,
        }
