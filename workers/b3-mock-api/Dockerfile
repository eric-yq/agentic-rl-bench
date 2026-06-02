FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py /app/app.py

EXPOSE 8003
ENV UVICORN_WORKERS=4
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port 8003 --workers ${UVICORN_WORKERS}"]
