# Build context = repo root: docker build -f integrations/handoff/Dockerfile .
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TENANTGUARD_ROOT=/app \
    HANDOFF_STATE_PATH=/data/handoff-state.json

WORKDIR /app
COPY common /app/common
RUN pip install --no-cache-dir /app/common
COPY integrations/handoff/requirements.txt /app/integrations/handoff/requirements.txt
RUN pip install --no-cache-dir -r /app/integrations/handoff/requirements.txt

COPY tenantguard.yaml /app/tenantguard.yaml
COPY integrations/handoff/tenantguard_handoff /app/integrations/handoff/tenantguard_handoff
COPY integrations/handoff/tools /app/integrations/handoff/tools
RUN mkdir -p /data

WORKDIR /app/integrations/handoff
EXPOSE 8400
HEALTHCHECK --interval=10s --timeout=3s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8400/healthz', timeout=2).status == 200 else 1)"
CMD ["python", "-m", "tenantguard_handoff", "--host", "0.0.0.0", "--port", "8400"]
