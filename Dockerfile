FROM python:3.11-slim

WORKDIR /app

# Install system deps and LibreOffice
RUN apt-get update && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        gcc \
        libpq-dev \
        libreoffice-writer && \
    rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create uploads directory
RUN mkdir -p /app/uploads

# We remove the EXPOSE instruction as Railway handles it dynamically

# Binds IPv6 because Railway's private network is IPv6-only, which is how
# Prometheus reaches /metrics. A :: socket still accepts IPv4, so public
# traffic through Railway's proxy is unaffected.
CMD ["sh", "-c", "uvicorn app.main:app --host :: --port $PORT"]