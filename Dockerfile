FROM python:3.11-slim

WORKDIR /app

# Install system deps for psycopg2-binary and general build tools
RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc libpq-dev && \
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

# Use shell form (no brackets) to natively evaluate the $PORT variable 
# and remove the dangerous --reload flag
CMD uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}