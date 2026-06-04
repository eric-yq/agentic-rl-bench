# B5 worker - DuckDB in-process with TPC-H workload.
#
# Multi-arch (linux/amd64 + linux/arm64). DuckDB ships official wheels
# for both architectures; on aarch64 the vectorized executor uses
# NEON / SVE which is the central c8g signal we want to capture.
#
# IMPORTANT: we pre-install the `tpch` extension at build time so the
# runtime container does not need outbound network access. Without
# this, the first request triggers a download from
# extensions.duckdb.org and warmup hangs in air-gapped / restricted
# VPCs (which is exactly what bench hosts often are).

FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Bake the tpch extension into ~/.duckdb/extensions/ so runtime is
# fully offline. INSTALL writes the extension binary to the image;
# LOAD verifies it links cleanly on this arch. We deliberately skip
# any post-LOAD query because the extension's `tpch_queries` table
# function isn't always materialised before a `dbgen` call - we let
# the runtime worker handle that under controlled timing.
RUN python -c "import duckdb; \
con = duckdb.connect(); \
con.execute('INSTALL tpch'); \
con.execute('LOAD tpch'); \
print('[build] tpch extension installed and loaded ok')"

COPY app.py /app/app.py

EXPOSE 8005

# Single uvicorn process: DuckDB's in-memory database can't be shared
# across processes. Concurrency comes from asyncio.to_thread + DuckDB's
# internal multi-threaded executor (PRAGMA threads=auto by default).
ENV TPCH_SF=1.0
ENV DUCKDB_THREADS=0
ENV DUCKDB_MEM_LIMIT=

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8005", "--workers", "1"]
