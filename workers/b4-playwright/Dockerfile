# Multi-arch (linux/amd64, linux/arm64) Playwright + Chromium-only.
# We deliberately use node:20-bookworm-equivalent python base and install
# only Chromium to avoid known ARM64 gaps in Firefox/WebKit images.
FROM python:3.11-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl \
        fonts-noto-cjk fonts-noto-color-emoji \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && playwright install --with-deps chromium

COPY app.py /app/app.py

# We listen on B4_PORT_BASE..B4_PORT_BASE+N-1; on a user-defined
# docker network all container ports are reachable peer-to-peer
# without an EXPOSE / publish step.

# MAX_CONTEXTS=0 (or unset) -> auto = 4 contexts per worker process.
# Set to a fixed integer to cap (e.g. for fairness across archs).
ENV MAX_CONTEXTS=0

# One uvicorn process per port (no SO_REUSEPORT). The client picks a
# port deterministically per request, so load is exactly even across
# Chromium masters - measured aarch64 SO_REUSEPORT can pile 35% of
# requests on one PID and starve others to 1%, which cripples c8g
# even though chromium itself is faster than on c7i.
#
# Sizing rationale (verified by `B4_TRACE=1` measurements):
#   - WORKERS = vCPU/2. Each Chromium master needs a real CPU and
#     competes with renderer threads; oversubscribing thrashes the
#     scheduler and CPU% drops by 4-5x.
#   - MAX_CONTEXTS = 4. With client-side strict round-robin, c=32
#     spreads exactly 4 connections to each of 8 workers - perfect
#     fit, no queueing. Higher c overflows into the pool queue but
#     Chromium stays busy.
#   - Total ACTIVE Chromium renderer processes at c=32: 8 masters +
#     32 renderers = 40. (Compare: 16 ctx/worker would be 128 procs
#     on 16 vCPU, scheduler thrash.)
#
# Auto-scaling:
#   4xlarge   (16 vCPU): 8 workers x 4 ctx = 32 total slots
#   16xlarge  (64 vCPU): 32 workers x 4 ctx = 128 total slots
#   24xlarge  (96 vCPU): 48 workers x 4 ctx = 192 total slots
ENV B4_UVICORN_WORKERS=auto
ENV B4_PORT_BASE=8004

# Launch one independent uvicorn process per port. We `&` each into
# the background, then `wait` so the container stays alive as long
# as any worker does (and exits if all die).
CMD ["sh", "-c", "\
  if [ \"${B4_UVICORN_WORKERS}\" = auto ]; then \
    NPROC=$(nproc); \
    W=$((NPROC / 2)); \
    if [ \"${W}\" -lt 2 ]; then W=2; fi; \
  else \
    W=${B4_UVICORN_WORKERS}; \
  fi; \
  echo \"[b4] launching ${W} single-process workers on ports ${B4_PORT_BASE}..\"; \
  export B4_NUM_PORTS=${W}; \
  i=0; \
  while [ ${i} -lt ${W} ]; do \
    port=$((B4_PORT_BASE + i)); \
    echo \"[b4] starting uvicorn on :${port}\"; \
    uvicorn app:app --host 0.0.0.0 --port ${port} --workers 1 & \
    i=$((i + 1)); \
  done; \
  wait"]
