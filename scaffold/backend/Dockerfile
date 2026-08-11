FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv

# Dependencies first so code edits don't invalidate the layer cache.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY migrations ./migrations

# Run as non-root.
RUN useradd -u 10001 -m appuser && chown -R appuser /srv
USER appuser

EXPOSE 8000

# 0.0.0.0, not 127.0.0.1 — a localhost bind inside a container is unreachable from the
# platform's health checker, which is the single most common App Platform deploy failure.
# $PORT is injected by App Platform; the default matches docker-compose.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
