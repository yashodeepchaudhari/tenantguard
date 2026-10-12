# Build context = repo root (docker-compose.yml): docker build -f billing/Dockerfile .
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TENANTGUARD_ROOT=/app

WORKDIR /app
COPY common /app/common
RUN pip install --no-cache-dir /app/common
COPY billing/requirements.txt /app/billing/requirements.txt
RUN pip install --no-cache-dir -r /app/billing/requirements.txt

COPY tenantguard.yaml /app/tenantguard.yaml
COPY layouts /app/layouts
COPY billing/tenantguard_billing /app/billing/tenantguard_billing

WORKDIR /app/billing
EXPOSE 8600
HEALTHCHECK --interval=10s --timeout=3s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8600/healthz', timeout=2).status == 200 else 1)"
CMD ["python", "-m", "tenantguard_billing", "--host", "0.0.0.0", "--port", "8600"]
