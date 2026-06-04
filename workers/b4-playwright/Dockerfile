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

# Multi-process uvicorn so a single asyncio event loop / single
# Chromium IPC pipe doesn't bottleneck throughput. Each worker
# process owns its own browser + its own MAX_CONTEXTS quota, so
# total in-flight contexts = WORKERS * MAX_CONTEXTS.
#
# We keep WORKERS modest (default 4) because each worker spawns
# its own Chromium master + child renderers, costing ~150MB +
# ~30MB per active context. On a 4xlarge 16 GB host, 4 workers
# x 8 contexts = ~32 in-flight contexts at <2GB resident is a
# safe sweet spot. Override B4_UVICORN_WORKERS to scale up on
# bigger instances.
ENV B4_UVICORN_WORKERS=auto

CMD ["sh", "-c", "\
  if [ \"${B4_UVICORN_WORKERS}\" = auto ]; then \
    NPROC=$(nproc); \
    if [ \"${NPROC}\" -lt 4 ]; then W=1; \
    elif [ \"${NPROC}\" -lt 16 ]; then W=2; \
    elif [ \"${NPROC}\" -lt 32 ]; then W=4; \
    elif [ \"${NPROC}\" -lt 64 ]; then W=8; \
    else W=16; fi; \
  else \
    W=${B4_UVICORN_WORKERS}; \
  fi; \
  echo \"[b4] starting uvicorn with ${W} workers (MAX_CONTEXTS=${MAX_CONTEXTS} per worker)\"; \
  exec uvicorn app:app --host 0.0.0.0 --port 8004 --workers ${W}"]
