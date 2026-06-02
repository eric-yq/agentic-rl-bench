"""B4 worker - Playwright headless Chromium service.

Maintains a single browser process; per request creates a fresh
BrowserContext (isolated cookies/storage) and replays a step list.
This matches how Agentic-RL frameworks typically use Playwright:
one browser, many contexts.
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

MAX_CONTEXTS = int(os.getenv("MAX_CONTEXTS", "8"))

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
    steps_done: int


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": _browser is not None}


@app.post("/task", response_model=TaskResponse)
async def task(req: TaskRequest) -> TaskResponse:
    if _browser is None:
        raise HTTPException(503, "browser not ready")
    t0 = time.perf_counter()
    done = 0
    async with _sem:
        ctx = await _browser.new_context(viewport={"width": 1280, "height": 800})
        try:
            page = await ctx.new_page()
            for step in req.steps:
                a = step.action
                args = step.args
                if a == "goto":
                    path = args.get("path", "/")
                    await page.goto(req.target_url + path, wait_until="domcontentloaded")
                elif a == "click":
                    sel = args.get("selector")
                    try:
                        await page.click(sel, timeout=2000)
                    except Exception:
                        # selector not found is acceptable in synthetic pages
                        pass
                elif a == "scroll":
                    await page.evaluate(f"window.scrollBy(0, {int(args.get('y', 400))})")
                elif a == "type":
                    sel = args.get("selector", "input")
                    text = args.get("text", "")
                    try:
                        await page.fill(sel, text, timeout=2000)
                    except Exception:
                        pass
                elif a == "screenshot":
                    await page.screenshot(full_page=False, type="png")
                else:
                    raise HTTPException(400, f"unknown action: {a}")
                done += 1
        finally:
            await ctx.close()
    return TaskResponse(wall_ms=(time.perf_counter() - t0) * 1000.0, steps_done=done)
