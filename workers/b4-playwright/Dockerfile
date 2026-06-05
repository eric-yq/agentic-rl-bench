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

# Multi-process Chromium with a small pool per worker. Each uvicorn
# worker process owns its own Chromium master + a pre-created pool
# of MAX_CONTEXTS BrowserContexts. Total in-flight contexts =
# WORKERS * MAX_CONTEXTS.
#
# CRITICAL: WORKERS must stay <= vCPU. Each Chromium master takes
# a real CPU; running more masters than vCPUs makes the kernel
# scheduler thrash and DROPS observed CPU utilisation. Empirically
# 32 masters on 16 vCPU hit ~10% CPU usage from context-switch
# overhead alone - vCPU/2 is the sweet spot.
#
# Auto-scaling:
#   4xlarge   (16 vCPU): 8 workers x 4 ctx = 32 total slots
#   16xlarge  (64 vCPU): 32 workers x 4 ctx = 128 total slots
#   24xlarge  (96 vCPU): 48 workers x 4 ctx = 192 total slots
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
