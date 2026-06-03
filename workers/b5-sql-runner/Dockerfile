# B5 worker - DuckDB in-process with TPC-H workload.
#
# Multi-arch (linux/amd64 + linux/arm64). DuckDB ships official wheels
# for both architectures; on aarch64 the vectorized executor uses
# NEON / SVE which is the central c8g signal we want to capture.

FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py /app/app.py

EXPOSE 8005

# uvicorn workers > 1 isn't useful here: a single duckdb in-memory
# database can't be shared across processes. We expose concurrency
# through asyncio.to_thread + DuckDB's internal multi-threaded
# executor (PRAGMA threads).
ENV TPCH_SF=1.0
ENV DUCKDB_THREADS=0
ENV DUCKDB_MEM_LIMIT=

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8005", "--workers", "1"]
