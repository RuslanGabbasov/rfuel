FROM python:3.13-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code
COPY *.py .

# Data directory for persistent SQLite DB
VOLUME ["/data"]
ENV DATA_DIR=/data

# Run as non-root
RUN useradd -m -u 1000 botuser && mkdir -p /data && chown -R botuser:botuser /app /data
USER botuser

CMD ["python", "bot.py"]
