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

# Multi-process Chromium with single-slot workers. Each uvicorn
# worker process owns its own Chromium master + exactly one
# BrowserContext (MAX_CONTEXTS=1). The kernel's SO_REUSEPORT
# accept-balancing distributes load across workers - this is
# significantly more even on aarch64 than asyncio Semaphore-based
# pooling inside a single worker process.
#
# Total in-flight contexts = WORKERS (since MAX_CONTEXTS=1).
# Auto-scaling targets ~2x vCPU workers so a c=2*vCPU sweep saturates
# without queueing: 4xlarge -> 32 workers, 16xlarge -> 128 workers,
# 24xlarge -> 192 workers.
ENV B4_UVICORN_WORKERS=auto

CMD ["sh", "-c", "\
  if [ \"${B4_UVICORN_WORKERS}\" = auto ]; then \
    NPROC=$(nproc); \
    W=$((NPROC * 2)); \
    if [ \"${W}\" -lt 8 ]; then W=8; fi; \
  else \
    W=${B4_UVICORN_WORKERS}; \
  fi; \
  echo \"[b4] starting uvicorn with ${W} single-slot workers\"; \
  exec uvicorn app:app --host 0.0.0.0 --port 8004 --workers ${W}"]
