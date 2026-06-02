FROM python:3.11-slim

# docker CLI for B8 cold-start tests (sock mounted from host)
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl docker.io \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . /app

ENTRYPOINT ["python", "-m", "main"]
