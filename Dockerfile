FROM python:3.11-slim

# Security: run as non-root
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --shell /bin/bash appuser

WORKDIR /app

# Install dependencies first for Docker layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Ensure writable runtime directories exist, preserve static incidents for
# volume seeding, and mark the entrypoint script executable.
RUN cp -r /app/incidents /app/incidents_static \
    && mkdir -p incidents data \
    && chmod +x /app/docker-entrypoint.sh \
    && chown -R appuser:appuser /app

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s \
    CMD curl -f http://localhost:8000/health || exit 1

ENTRYPOINT ["/app/docker-entrypoint.sh"]
# Single uvicorn worker — concurrency is handled by asyncio inside the process.
# Multiple workers would each own a separate asyncio.Semaphore, breaking the
# MAX_CONCURRENT_INVESTIGATIONS limit set in main.py.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--log-level", "info"]
