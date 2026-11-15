# Production container for NHS-JobWatch
FROM python:3.11-slim

# Enforce unbuffered stdout/stderr and configure timezone
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Europe/London

# Install system dependencies including ca-certificates and tzdata
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies first for caching
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source code and configuration
COPY jobwatch/ /app/jobwatch/
COPY config/ /app/config/

# Ensure runtime directories exist
RUN mkdir -p /app/data /app/data/backups

# NOTE: Railway persistent volumes mount as root (UID 0).
# Running as root ensures write permissions to /app/data for SQLite database and WAL logs.

CMD ["python", "-m", "jobwatch.bot"]

