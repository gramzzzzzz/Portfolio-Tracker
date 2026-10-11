# syntax=docker/dockerfile:1

# ---------- 1. build the React UI ----------
FROM node:22-bookworm-slim AS ui
WORKDIR /ui
COPY frontend/package.json frontend/package-lock.json ./
RUN npm install -g npm@latest
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# ---------- 2. run tests against the real sample files ----------
FROM python:3.12-slim AS test
WORKDIR /app
COPY backend/requirements.txt backend/requirements-dev.txt ./
RUN pip install --no-cache-dir --timeout=120 --retries=10 -r requirements-dev.txt
COPY backend/ ./
RUN PYTHONPATH=. python -m pytest -q

# ---------- 3. runtime ----------
FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    PT_DATA_DIR=/data PT_STATIC_DIR=/app/static
WORKDIR /app
COPY backend/requirements.txt ./
RUN pip install --no-cache-dir --timeout=120 --retries=10 -r requirements.txt \
 && groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid 10001 --home /app app \
 && mkdir -p /data && chown app:app /data
COPY backend/app ./app
COPY --from=ui /ui/dist ./static
# make sure the test stage ran (and passed) before an image is produced
COPY --from=test /app/requirements.txt /tmp/tested
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
# starts as root, fixes /data ownership, then runs as uid PUID (default 10001)
ENTRYPOINT ["docker-entrypoint.sh"]
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health',timeout=4).status==200 else 1)"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
