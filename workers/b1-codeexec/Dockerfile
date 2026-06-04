FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py /app/app.py

EXPOSE 8001

# B1 forks a fresh `python -c` subprocess per request. Each request is
# IO-bound on the asyncio event loop (waiting for the child to finish),
# so a single uvicorn worker can already drive many concurrent forks -
# but on 16+ vCPU instances we want multiple uvicorn processes too so
# the syscall + JSON encode overhead is parallelised. Default to
# matching the container's vCPU allocation.
ENV B1_UVICORN_WORKERS=auto

CMD ["sh", "-c", "\
  if [ \"${B1_UVICORN_WORKERS}\" = auto ]; then \
    W=$(nproc); \
  else \
    W=${B1_UVICORN_WORKERS}; \
  fi; \
  echo \"[b1] starting uvicorn with ${W} workers\"; \
  exec uvicorn app:app --host 0.0.0.0 --port 8001 --workers ${W}"]
