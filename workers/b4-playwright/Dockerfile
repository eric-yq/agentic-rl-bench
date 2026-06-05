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

EXPOSE 8004
# MAX_CONTEXTS=0 (or unset) -> auto = 2x container vCPU.
# Set to a fixed integer to cap (e.g. for fairness across archs).
ENV MAX_CONTEXTS=0

# Multi-process Chromium so a single asyncio event loop / single
# Chromium IPC pipe doesn't bottleneck throughput. Each worker
# process owns its own browser + a pre-created context pool of
# MAX_CONTEXTS slots, so total in-flight contexts =
# WORKERS * MAX_CONTEXTS but the renderer process count is FIXED
# at startup (no fork storm under load).
#
# Auto-scaling targets ~vCPU/2 workers so each Chromium master gets
# its own core: on 4xlarge -> 8 workers x 4 ctx = 32 contexts;
# on 16xlarge -> 32 workers x 4 ctx = 128 contexts; etc.
ENV B4_UVICORN_WORKERS=auto

CMD ["sh", "-c", "\
  if [ \"${B4_UVICORN_WORKERS}\" = auto ]; then \
    NPROC=$(nproc); \
    if [ \"${NPROC}\" -lt 4 ]; then W=2; \
    elif [ \"${NPROC}\" -lt 8 ]; then W=4; \
    elif [ \"${NPROC}\" -lt 16 ]; then W=6; \
    elif [ \"${NPROC}\" -lt 32 ]; then W=8; \
    elif [ \"${NPROC}\" -lt 64 ]; then W=16; \
    else W=32; fi; \
  else \
    W=${B4_UVICORN_WORKERS}; \
  fi; \
  echo \"[b4] starting uvicorn with ${W} workers (MAX_CONTEXTS=${MAX_CONTEXTS} per worker)\"; \
  exec uvicorn app:app --host 0.0.0.0 --port 8004 --workers ${W}"]
