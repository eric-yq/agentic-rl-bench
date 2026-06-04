FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential ca-certificates \
        util-linux \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py /app/app.py

EXPOSE 8001

# B1 forks a fresh `python -c` subprocess per request (wrapped in
# `prlimit` so we don't have to use asyncio's slow preexec_fn path,
# which would cap throughput at ~c=32 due to CPython's fork-helper
# lock). Multiple uvicorn worker processes parallelise the syscall +
# JSON encode overhead. Default to one worker per container vCPU.
ENV B1_UVICORN_WORKERS=auto

CMD ["sh", "-c", "\
  if [ \"${B1_UVICORN_WORKERS}\" = auto ]; then \
    W=$(nproc); \
  else \
    W=${B1_UVICORN_WORKERS}; \
  fi; \
  echo \"[b1] starting uvicorn with ${W} workers\"; \
  exec uvicorn app:app --host 0.0.0.0 --port 8001 --workers ${W}"]
