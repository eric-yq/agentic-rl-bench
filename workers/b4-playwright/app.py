"""B4 worker - Playwright headless Chromium service.

Multi-process model: each uvicorn worker owns its own Chromium
browser instance, isolating it from the others. Within a worker,
each request gets a fresh BrowserContext (cookies / storage isolated).

Why multi-process? A single Chromium IPC pipe + a single asyncio
event loop becomes the throughput ceiling well before we saturate
the host's CPU. Splitting Chromium across N worker processes lets
N event loops run on N cores in parallel, multiplying real CPU
utilisation from ~1 core to ~N cores. The trade-off is RAM:
each worker boots its own Chromium master (~150MB) plus renderer
processes per active context.

Knobs (env):
  - B4_UVICORN_WORKERS  : how many uvicorn processes (= chromium
                          masters). Auto-scaled by the launch script
                          to vCPU / 4 (with floors / ceilings).
  - MAX_CONTEXTS        : per-process BrowserContext concurrency cap.
                          Total in-flight contexts = WORKERS * MAX_CONTEXTS.
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
    """Default per-uvicorn-worker context cap.

    Each uvicorn worker process owns its own Chromium browser; total
    in-flight contexts = WORKERS * MAX_CONTEXTS. We pick 8 here because
    the launch script defaults WORKERS to ~vCPU/4, so vCPU/4 * 8 = 2x
    vCPU total contexts - the same target as the prior single-worker
    config, but now spread across multiple Chromium master processes
    so the asyncio event loop is not a serial chokepoint.

    Override with MAX_CONTEXTS env to cap (e.g. for cross-arch fairness
    or memory-constrained instances).
    """
    return 8


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
