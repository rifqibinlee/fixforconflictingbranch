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
    PATH="/opt/venv/bin:$PATH"

WORKDIR /app

# Install ONLY the runtime libraries needed to execute (no compilers!)
# libpq5 is the runtime equivalent of libpq-dev
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    postgresql-client \
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
