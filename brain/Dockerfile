# Build context = repo root (docker-compose.yml): docker build -f brain/Dockerfile .
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TENANTGUARD_ROOT=/app

WORKDIR /app
COPY common /app/common
RUN pip install --no-cache-dir /app/common
COPY brain/requirements.txt /app/brain/requirements.txt
RUN pip install --no-cache-dir -r /app/brain/requirements.txt

COPY tenantguard.yaml /app/tenantguard.yaml
COPY layouts /app/layouts
COPY brain/tenantguard_brain /app/brain/tenantguard_brain
COPY brain/tools /app/brain/tools

WORKDIR /app/brain
EXPOSE 8200
HEALTHCHECK --interval=10s --timeout=3s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8200/healthz', timeout=2).status == 200 else 1)"
CMD ["python", "-m", "tenantguard_brain"]
