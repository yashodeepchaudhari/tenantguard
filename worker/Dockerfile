# Build context = repo root (docker-compose.yml): docker build -f worker/Dockerfile .
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TENANTGUARD_ROOT=/app \
    WORKER_DATA_DIR=/data \
    MALLOC_ARENA_MAX=2 \
    MALLOC_MMAP_THRESHOLD_=131072

WORKDIR /app
COPY common /app/common
RUN pip install --no-cache-dir /app/common
COPY worker/requirements.txt /app/worker/requirements.txt
RUN pip install --no-cache-dir -r /app/worker/requirements.txt

COPY tenantguard.yaml /app/tenantguard.yaml
COPY layouts /app/layouts
COPY worker/app /app/worker/app
COPY worker/tools /app/worker/tools
RUN mkdir -p /data

WORKDIR /app/worker
EXPOSE 8100
HEALTHCHECK --interval=10s --timeout=3s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8100/healthz', timeout=2).status == 200 else 1)"
CMD ["python", "-m", "app", "--host", "0.0.0.0", "--port", "8100"]
