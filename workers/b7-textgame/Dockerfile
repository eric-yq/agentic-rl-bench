# B7 worker - ALFWorld/TextWorld-style text simulator.
#
# Pure Python, no native deps - same image runs cleanly on linux/amd64
# and linux/arm64. The signal we capture is exactly what the original
# B7 design called for: per-step latency of tight Python state-update
# loops + GIL contention across uvicorn workers.

FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py /app/app.py

EXPOSE 8007

# Multi-process uvicorn so we exploit all vCPUs of the container.
# Python GIL pins a single worker process to one core, so without
# many workers we cap at 1/vCPU utilisation. Default = nproc; on a
# 4xlarge we get 16 workers, on 16xlarge 64, etc.
ENV B7_UVICORN_WORKERS=auto

CMD ["sh", "-c", "\
  if [ \"${B7_UVICORN_WORKERS}\" = auto ]; then \
    W=$(nproc); \
  else \
    W=${B7_UVICORN_WORKERS}; \
  fi; \
  echo \"[b7] starting uvicorn with ${W} workers\"; \
  exec uvicorn app:app --host 0.0.0.0 --port 8007 --workers ${W}"]
