"""B1 worker - executes Python snippets in a fresh subprocess.

POST /run {code: str, timeout: int} -> {exit_code, stdout, stderr, wall_ms}

Each request spawns `python -c <code>` with resource limits.
This mirrors how a real Agentic-RL sandbox runs LLM-generated code
(short-lived subprocess, isolated stdout/stderr, hard timeout).
"""

from __future__ import annotations

import asyncio
import resource
import time

from fastapi import FastAPI
from pydantic import BaseModel, Field

app = FastAPI()


class RunRequest(BaseModel):
    code: str = Field(..., max_length=64_000)
    timeout: int = Field(5, ge=1, le=60)


class RunResponse(BaseModel):
    exit_code: int
    stdout: str
    stderr: str
    wall_ms: float
    timed_out: bool = False


def _set_limits() -> None:
    # 256 MB RSS, 30s CPU, no core dumps
    resource.setrlimit(resource.RLIMIT_AS, (256 * 1024 * 1024, 256 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_CPU, (30, 30))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True}


@app.post("/run", response_model=RunResponse)
async def run(req: RunRequest) -> RunResponse:
    t0 = time.perf_counter()
    proc = await asyncio.create_subprocess_exec(
        "python3", "-c", req.code,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        preexec_fn=_set_limits,
    )
    timed_out = False
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=req.timeout)
        rc = proc.returncode if proc.returncode is not None else -1
    except asyncio.TimeoutError:
        timed_out = True
        proc.kill()
        try:
            out, err = await proc.communicate()
        except Exception:
            out, err = b"", b""
        rc = -9

    wall_ms = (time.perf_counter() - t0) * 1000.0
    return RunResponse(
        exit_code=rc,
        stdout=out[:4096].decode("utf-8", errors="replace"),
        stderr=err[:4096].decode("utf-8", errors="replace"),
        wall_ms=wall_ms,
        timed_out=timed_out,
    )
