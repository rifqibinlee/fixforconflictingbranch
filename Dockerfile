# ==========================================
# STAGE 1: Builder (Heavy, temporary container)
# ==========================================
FROM python:3.11-slim-bookworm AS builder

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    build-essential \
    python3-dev \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ==========================================
# STAGE 2: Production (Lightweight, final container)
# ==========================================
FROM python:3.11-slim-bookworm

ENV PYTHONFAULTHANDLER=1 \
    PYTHONUNBUFFERED=1 \
    FLASK_APP=app.py \
    FLASK_RUN_HOST=0.0.0.0 \
    FLASK_RUN_PORT=5000 \
    PATH="/opt/venv/bin:$PATH" \
    QT_QPA_PLATFORM=offscreen \
    QGIS_PREFIX_PATH=/usr \
    PYTHONPATH="/usr/share/qgis/python:/usr/share/qgis/python/plugins:/usr/lib/python3/dist-packages"

WORKDIR /app

# Install runtime libs + QGIS from Debian Bookworm's own repos (supports ARM64)
# No need for external QGIS repo — Bookworm ships QGIS 3.34.x
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    postgresql-client \
    qgis-server \
    python3-qgis \
    xvfb \
    && rm -rf /var/lib/apt/lists/*

# Copy the pre-compiled Python packages from the builder stage
COPY --from=builder /opt/venv /opt/venv

# Create necessary directories
RUN mkdir -p /app/templates /app/cd_cache

# Copy the rest of your application code
COPY . .
RUN chmod +x docker-entrypoint.sh

EXPOSE 5000

ENTRYPOINT ["/app/docker-entrypoint.sh"]
