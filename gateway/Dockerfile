# Build context = repo root (docker-compose.yml): docker build -f gateway/Dockerfile .
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TENANTGUARD_ROOT=/app

WORKDIR /app
COPY common /app/common
RUN pip install --no-cache-dir /app/common
COPY gateway/requirements.txt /app/gateway/requirements.txt
RUN pip install --no-cache-dir -r /app/gateway/requirements.txt

COPY tenantguard.yaml /app/tenantguard.yaml
COPY worker/calibration.json /app/worker/calibration.json
COPY gateway/tenantguard_gateway /app/gateway/tenantguard_gateway

WORKDIR /app/gateway
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2).status == 200 else 1)"
CMD ["python", "-m", "tenantguard_gateway", "--host", "0.0.0.0", "--port", "8000"]
