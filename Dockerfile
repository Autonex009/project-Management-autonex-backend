FROM python:3.11-slim

WORKDIR /app

# Pre-accept Microsoft Font EULA so the Docker build doesn't hang
RUN echo "ttf-mscorefonts-installer msttcorefonts/accepted-mscorefonts-eula select true" | debconf-set-selections

# Install system deps, LibreOffice, and Microsoft Core Fonts
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        gcc \
        libpq-dev \
        libreoffice-core-nogui \
        libreoffice-writer \
        ttf-mscorefonts-installer && \
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

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port $PORT"]