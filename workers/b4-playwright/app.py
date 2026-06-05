"""B4 worker - Playwright headless Chromium service.

Multi-process + context-pool model:
  - Each uvicorn worker process owns its own Chromium browser.
  - Within a worker, we pre-create a pool of `MAX_CONTEXTS`
    long-lived BrowserContexts at startup. Per-request we borrow
    one, run the trajectory, then `clear_cookies()` + navigate
    back to `about:blank` before returning it.

Why pool instead of per-request context? Creating + tearing down a
BrowserContext spawns / kills a Chromium renderer process on every
request. At c=128 that's hundreds of fork()/wait() cycles per
second; aarch64 kernels in particular bottleneck on this fork
storm long before any V8 / Skia work is done. Pooling keeps the
renderer set fixed at WORKERS * MAX_CONTEXTS, so CPU utilisation
scales with concurrency instead of choking on fork overhead.

Knobs (env):
  - B4_UVICORN_WORKERS  : how many uvicorn processes (= chromium
                          masters). Auto-scaled by the launch script.
  - MAX_CONTEXTS        : per-process pool size (= renderer count
                          per Chromium master). Total in-flight
                          contexts = WORKERS * MAX_CONTEXTS.
  - ACTION_TIMEOUT_MS   : per-action timeout for click / fill. Small
                          value (default 500ms) so a missed selector
                          fails fast instead of dominating wall time.
"""

from __future__ import annotations

import asyncio
import os
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException
from playwright.async_api import (
    async_playwright,
    Browser,
    BrowserContext,
    Page,
)
from pydantic import BaseModel


def _default_max_contexts() -> int:
    """Per-uvicorn-worker context-pool size.

    Default to 8 so each worker can absorb load imbalance from the
    kernel's SO_REUSEPORT TCP accept distribution. With WORKERS =
    vCPU/2 = 8 on a 4xlarge, total slots = 64; for a c=32 sweep that
    means even a worst-case hash that piles 16 connections onto one
    worker still has half its slots free, keeping pool_wait < 1s.

    Chromium master process count = WORKERS, NOT WORKERS * MAX_CONTEXTS;
    the master fans out to lightweight renderer processes per active
    context, but renderers are mostly IPC-blocked so total active
    CPU is bounded by master count. WORKERS must stay <= vCPU.
    """
    return 8


MAX_CONTEXTS = int(os.getenv("MAX_CONTEXTS", "0")) or _default_max_contexts()
ACTION_TIMEOUT_MS = int(os.getenv("ACTION_TIMEOUT_MS", "500"))
GOTO_TIMEOUT_MS = int(os.getenv("GOTO_TIMEOUT_MS", "5000"))

_browser: Browser | None = None
_pw = None
# Async queue acts both as the pool and as a bounded semaphore: a
# request can only run when it can `get()` a context, and `put()`
# returns it. No separate Semaphore needed.
_pool: asyncio.Queue[tuple[BrowserContext, Page]] | None = None


async def _make_slot(browser: Browser) -> tuple[BrowserContext, Page]:
    ctx = await browser.new_context(viewport={"width": 1280, "height": 800})
    page = await ctx.new_page()
    return ctx, page


async def _reset_slot(ctx: BrowserContext, page: Page) -> tuple[BrowserContext, Page]:
    """Clear per-trajectory state so the slot looks fresh next time.

    On error we drop the slot and create a new one (Chromium pages can
    end up wedged after navigation timeouts; better to recycle).
    """
    try:
        await ctx.clear_cookies()
        await page.goto("about:blank", wait_until="domcontentloaded",
                        timeout=GOTO_TIMEOUT_MS)
        return ctx, page
    except Exception:
        try:
            await ctx.close()
        except Exception:
            pass
        if _browser is None:
            raise
        return await _make_slot(_browser)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _browser, _pw, _pool
    _pw = await async_playwright().start()
    _browser = await _pw.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--disable-features=IsolateOrigins,site-per-process",
            # Skia / GPU compositing off so we don't accidentally
            # depend on hardware accel that's missing in containers.
            "--disable-software-rasterizer",
        ],
    )
    print(f"[b4] worker pid={os.getpid()} pool={MAX_CONTEXTS} "
          f"action_timeout={ACTION_TIMEOUT_MS}ms", flush=True)

    _pool = asyncio.Queue(maxsize=MAX_CONTEXTS)
    # Pre-create all slots up front so the first burst doesn't pay
    # the renderer-start tax.
    for _ in range(MAX_CONTEXTS):
        slot = await _make_slot(_browser)
        await _pool.put(slot)
    print(f"[b4] worker pid={os.getpid()} pool ready", flush=True)

    yield

    # Drain the pool on shutdown.
    while not _pool.empty():
        ctx, _page = _pool.get_nowait()
        try:
            await ctx.close()
        except Exception:
            pass
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
    pool_size = _pool.qsize() if _pool is not None else 0
    return {"ok": _browser is not None and _pool is not None,
            "pool_idle": pool_size,
            "pool_max": MAX_CONTEXTS}


@app.post("/task", response_model=TaskResponse)
async def task(req: TaskRequest) -> TaskResponse:
    if _browser is None or _pool is None:
        raise HTTPException(503, "browser not ready")
    t0 = time.perf_counter()
    done = 0
    failed = 0

    # Optional per-action timing instrumentation. Set B4_TRACE=1 in the
    # worker env (compose) and inspect `docker logs arl-b4-worker` to
    # see where time goes. KEEP OFF in production - even just the time
    # measurements add ~5us per action.
    trace = os.getenv("B4_TRACE", "0") == "1"
    pool_wait_t0 = time.perf_counter()
    ctx, page = await _pool.get()
    pool_wait_ms = (time.perf_counter() - pool_wait_t0) * 1000.0

    action_times: list[tuple[str, float]] = []
    try:
        for step in req.steps:
            a = step.action
            args = step.args
            a_t0 = time.perf_counter()
            try:
                if a == "goto":
                    path = args.get("path", "/")
                    await page.goto(req.target_url + path,
                                    wait_until="domcontentloaded",
                                    timeout=GOTO_TIMEOUT_MS)
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
                # Selector misses / per-action timeouts: count + move on.
                failed += 1
            if trace:
                action_times.append(
                    (a, (time.perf_counter() - a_t0) * 1000.0)
                )
    finally:
        # Reset and return to pool. _reset_slot recreates on error so
        # we don't shrink the pool.
        try:
            ctx, page = await _reset_slot(ctx, page)
        except Exception:
            # Last-resort: if even slot recreation fails, close the
            # broken context and let the pool size shrink. The pool
            # will eventually refill via successful resets.
            try:
                await ctx.close()
            except Exception:
                pass
            ctx = page = None  # type: ignore
        if ctx is not None:
            await _pool.put((ctx, page))

    wall_ms = (time.perf_counter() - t0) * 1000.0
    if trace:
        # One line per trajectory; histogram by action type.
        per_kind: dict[str, list[float]] = {}
        for kind, ms in action_times:
            per_kind.setdefault(kind, []).append(ms)
        summary = " ".join(
            f"{k}={sum(v):.0f}ms/{len(v)}" for k, v in per_kind.items()
        )
        print(f"[b4-trace] wall={wall_ms:.0f}ms pool_wait={pool_wait_ms:.0f}ms "
              f"done={done} failed={failed} {summary}",
              flush=True)

    return TaskResponse(
        wall_ms=wall_ms,
        steps_done=done,
        steps_failed=failed,
        actions=len(req.steps),
    )
