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

# Multi-process Chromium with a generous pool per worker. Each
# uvicorn worker process owns its own Chromium master + a pre-
# created pool of MAX_CONTEXTS BrowserContexts.
#
# Sizing rationale (verified by `B4_TRACE=1` measurements on c=32):
#   - WORKERS must stay <= vCPU/2. Each master takes a real CPU
#     and competes with renderer threads; oversubscribing makes the
#     scheduler thrash and CPU% drops by 4-5x.
#   - MAX_CONTEXTS = 16 absorbs the worst-case SO_REUSEPORT hash
#     imbalance. Even when the kernel hashes ALL 16 of the 32 client
#     connections onto a single worker, that worker has 16 slots so
#     no queueing occurs (pool_wait approx 0).
#   - Total slots = WORKERS * 16.
#
# Auto-scaling:
#   4xlarge   (16 vCPU): 8 workers x 16 ctx = 128 total slots
#   16xlarge  (64 vCPU): 32 workers x 16 ctx = 512 total slots
#   24xlarge  (96 vCPU): 48 workers x 16 ctx = 768 total slots
ENV B4_UVICORN_WORKERS=auto

CMD ["sh", "-c", "\
  if [ \"${B4_UVICORN_WORKERS}\" = auto ]; then \
    NPROC=$(nproc); \
    W=$((NPROC / 2)); \
    if [ \"${W}\" -lt 2 ]; then W=2; fi; \
  else \
    W=${B4_UVICORN_WORKERS}; \
  fi; \
  echo \"[b4] starting uvicorn with ${W} workers (MAX_CONTEXTS=${MAX_CONTEXTS} per worker)\"; \
  exec uvicorn app:app --host 0.0.0.0 --port 8004 --workers ${W}"]
