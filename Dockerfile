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

# Must stay IPv4. Binding :: does NOT give a dual-stack socket here: asyncio
# sets IPV6_V6ONLY on AF_INET6 sockets in create_server, so `--host ::` listens
# on IPv6 only and Railway's public proxy (IPv4) gets no origin response.
# Prometheus therefore scrapes this service over its public domain, not the
# private network — see monitoring/prometheus/prometheus.yml.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port $PORT"]