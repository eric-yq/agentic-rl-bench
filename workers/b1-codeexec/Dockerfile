FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py /app/app.py

EXPOSE 8001
ENV WORKERS=4
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port 8001 --workers ${WORKERS}"]
