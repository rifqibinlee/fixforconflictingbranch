# ==========================================
# STAGE 1: Builder (Heavy, temporary container)
# ==========================================
FROM python:3.12-slim AS builder

WORKDIR /app

# Install heavy C-compilers and development headers
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    build-essential \
    python3-dev \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Create a virtual environment so we can easily copy all installed packages later
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Install Python packages into the virtual environment
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ==========================================
# STAGE 2: Production (Lightweight, final container)
# ==========================================
FROM python:3.12-slim

ENV PYTHONFAULTHANDLER=1 \
    PYTHONUNBUFFERED=1 \
    FLASK_APP=app.py \
    FLASK_RUN_HOST=0.0.0.0 \
    FLASK_RUN_PORT=5000 \
    PATH="/opt/venv/bin:$PATH" \
    QT_QPA_PLATFORM=offscreen \
    QGIS_PREFIX_PATH=/usr \
    PYTHONPATH="/usr/share/qgis/python:/usr/share/qgis/python/plugins:/usr/lib/python3/dist-packages:$PYTHONPATH"

WORKDIR /app

# Install runtime libraries + QGIS + PyQGIS
# 1) libpq5 for PostgreSQL
# 2) gnupg/wget to add QGIS repo
# 3) qgis-server + python3-qgis for headless processing
# 4) xvfb for virtual framebuffer (needed by wedge_buffer etc.)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    postgresql-client \
    gnupg \
    wget \
    && mkdir -m755 -p /etc/apt/keyrings \
    && wget -q -O /etc/apt/keyrings/qgis-archive-keyring.gpg \
       https://download.qgis.org/downloads/qgis-archive-keyring.gpg \
    && echo "Types: deb deb-src\nURIs: https://qgis.org/debian\nSuites: bookworm\nArchitectures: amd64\nComponents: main\nSigned-By: /etc/apt/keyrings/qgis-archive-keyring.gpg" \
       > /etc/apt/sources.list.d/qgis.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
       qgis-server \
       python3-qgis \
       xvfb \
    && apt-get purge -y gnupg wget \
    && apt-get autoremove -y \
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
