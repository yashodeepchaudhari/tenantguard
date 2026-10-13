# Build context = repo root (see docker-compose.yml, service `lab`).
FROM python:3.12-slim
WORKDIR /app
COPY common /app/common
RUN pip install --no-cache-dir -e /app/common
COPY lab/requirements.txt /app/lab/requirements.txt
RUN pip install --no-cache-dir -r /app/lab/requirements.txt
COPY tenantguard.yaml /app/tenantguard.yaml
COPY lab /app/lab
WORKDIR /app/lab
ENV TENANTGUARD_ROOT=/app PYTHONUNBUFFERED=1
EXPOSE 8300
CMD ["python", "-m", "tenantguard_lab", "serve", "--host", "0.0.0.0", "--port", "8300"]
