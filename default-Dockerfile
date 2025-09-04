FROM python:3.13-slim

# Install required system packages
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    git \
    ffmpeg \
    curl \
    procps \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Clone your GitHub repo directly
RUN git clone https://github.com/Empire-of-Shadows/Codex.git .
COPY healthcheck.py /app/healthcheck.py
RUN chmod +x /app/healthcheck.py

# Install Python dependencies
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Create logs directory
RUN mkdir -p logs

# Add environment variables
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

CMD ["python", "codex.py"]
