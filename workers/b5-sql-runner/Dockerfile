# Optional helper container that pre-seeds the postgres database.
# Idempotent; the orchestrator's b5 runner will also seed on first call.
FROM python:3.11-slim

RUN pip install --no-cache-dir asyncpg==0.29.0
WORKDIR /app
COPY seed.py /app/seed.py
CMD ["python", "/app/seed.py"]
