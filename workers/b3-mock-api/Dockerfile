FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        procps iproute2 curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py /app/app.py

EXPOSE 8003

# Auto-scale uvicorn worker count to the container's vCPU allocation.
# B3 is heavily IO-blocked on SQLite (in-memory but C-level mutex);
# matching workers to cores lets us actually saturate the CPU at high
# concurrency. Override with B3_UVICORN_WORKERS in the compose file
# if you want to clamp it for fairness across architectures.
ENV B3_UVICORN_WORKERS=auto

CMD ["sh", "-c", "\
  if [ \"${B3_UVICORN_WORKERS}\" = auto ]; then \
    W=$(nproc); \
  else \
    W=${B3_UVICORN_WORKERS}; \
  fi; \
  echo \"[b3] starting uvicorn with ${W} workers\"; \
  exec uvicorn app:app --host 0.0.0.0 --port 8003 --workers ${W}"]
