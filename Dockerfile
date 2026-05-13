FROM python:3.11-slim

# Security: run as non-root
RUN useradd --create-home --shell /bin/bash appuser

WORKDIR /app

# Install dependencies first for Docker layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Ensure incidents directory exists
RUN mkdir -p incidents && chown -R appuser:appuser /app

USER appuser

EXPOSE 8000

# Uvicorn with single worker (use gunicorn + uvicorn workers for prod scale)
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2", "--log-level", "info"]
