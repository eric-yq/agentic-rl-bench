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
# MAX_CONTEXTS=0 (or unset) -> auto = container vCPU count.
# Set to a fixed integer to cap (e.g. for fairness across archs).
ENV MAX_CONTEXTS=0
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8004", "--workers", "1"]
