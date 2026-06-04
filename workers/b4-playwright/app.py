"""B4 worker - Playwright headless Chromium service.

One persistent browser, per-request fresh BrowserContext (isolated
cookies/storage). Each request replays a step list and reports per-
step success so the orchestrator can compute a selector miss rate.

`MAX_CONTEXTS` defaults to the container's vCPU count - one context
per core saturates Chromium's renderer threads without thrashing.
On a 4xlarge that's 16; on 16xlarge 64; on 24xlarge 96.
"""

from __future__ import annotations

import asyncio
import os
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException
from playwright.async_api import async_playwright, Browser
from pydantic import BaseModel


def _default_max_contexts() -> int:
    """One context per vCPU. Honours cgroup CPU quota when present."""
    try:
        # Match docker --cpus / cgroup quota (e.g. compose `cpus: 16`).
        return max(1, len(os.sched_getaffinity(0)))
    except Exception:
        return os.cpu_count() or 8


MAX_CONTEXTS = int(os.getenv("MAX_CONTEXTS", "0")) or _default_max_contexts()
ACTION_TIMEOUT_MS = int(os.getenv("ACTION_TIMEOUT_MS", "2000"))

_browser: Browser | None = None
_pw = None
_sem = asyncio.Semaphore(MAX_CONTEXTS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _browser, _pw
    _pw = await async_playwright().start()
    _browser = await _pw.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--disable-features=IsolateOrigins,site-per-process",
        ],
    )
    yield
    await _browser.close()
    await _pw.stop()


app = FastAPI(lifespan=lifespan)


class Step(BaseModel):
    action: str
    args: dict[str, Any] = {}


class TaskRequest(BaseModel):
    target_url: str
    steps: list[Step]


class TaskResponse(BaseModel):
    wall_ms: float
    steps_done: int       # steps that ran without throwing
    steps_failed: int     # selector misses, type/click/wait timeouts
    actions: int          # total step count requested


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": _browser is not None}


@app.post("/task", response_model=TaskResponse)
async def task(req: TaskRequest) -> TaskResponse:
    if _browser is None:
        raise HTTPException(503, "browser not ready")
    t0 = time.perf_counter()
    done = 0
    failed = 0
    async with _sem:
        ctx = await _browser.new_context(viewport={"width": 1280, "height": 800})
        try:
            page = await ctx.new_page()
            for step in req.steps:
                a = step.action
                args = step.args
                try:
                    if a == "goto":
                        path = args.get("path", "/")
                        await page.goto(req.target_url + path,
                                        wait_until="domcontentloaded",
                                        timeout=10_000)
                    elif a == "click":
                        sel = args.get("selector")
                        await page.click(sel, timeout=ACTION_TIMEOUT_MS)
                    elif a == "scroll":
                        await page.evaluate(
                            f"window.scrollBy(0, {int(args.get('y', 400))})"
                        )
                    elif a == "type":
                        sel = args.get("selector", "input")
                        text = args.get("text", "")
                        await page.fill(sel, text, timeout=ACTION_TIMEOUT_MS)
                    elif a == "screenshot":
                        # Synthetic encode benchmark - keep PNG so Skia
                        # actually compresses (vs JPEG which is ~free).
                        await page.screenshot(full_page=False, type="png")
                    else:
                        raise HTTPException(400, f"unknown action: {a}")
                    done += 1
                except HTTPException:
                    raise
                except Exception:
                    # Selector misses, fill timeouts: real LLM agents
                    # also miss occasionally; record but don't abort.
                    failed += 1
        finally:
            await ctx.close()
    return TaskResponse(
        wall_ms=(time.perf_counter() - t0) * 1000.0,
        steps_done=done,
        steps_failed=failed,
        actions=len(req.steps),
    )
