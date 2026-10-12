# Build context = repo root: docker build -f narrator/Dockerfile .
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TENANTGUARD_ROOT=/app

WORKDIR /app
COPY common /app/common
RUN pip install --no-cache-dir /app/common
COPY narrator/requirements.txt /app/narrator/requirements.txt
RUN pip install --no-cache-dir -r /app/narrator/requirements.txt

COPY tenantguard.yaml /app/tenantguard.yaml
COPY layouts /app/layouts
COPY narrator/tenantguard_narrator /app/narrator/tenantguard_narrator

WORKDIR /app/narrator
EXPOSE 8700
HEALTHCHECK --interval=10s --timeout=3s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8700/healthz', timeout=2).status == 200 else 1)"
CMD ["python", "-m", "tenantguard_narrator", "--host", "0.0.0.0", "--port", "8700"]
