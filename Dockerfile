FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    DATA_DIR=/app/data \
    DEBIAN_FRONTEND=noninteractive

WORKDIR /app

# openssl 用于 hysteria2 自签名证书；curl 用于健康检查
RUN apt-get update && apt-get install -y --no-install-recommends \
    openssl curl \
    && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ ./backend/
COPY frontend/ ./frontend/

RUN mkdir -p /app/data

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -sf http://127.0.0.1:8080/ || exit 1

CMD ["uvicorn", "main:app", "--app-dir", "/app/backend", "--host", "0.0.0.0", "--port", "8080"]
