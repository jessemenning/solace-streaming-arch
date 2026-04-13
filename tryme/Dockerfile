FROM python:3.11-slim

WORKDIR /app

COPY tryme/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy server and UI
COPY tryme/server.py tryme/index.html ./
COPY backfill ./backfill

# Registry is mounted from project root at runtime via volume
# (config/topic-mv-registry.yaml → /app/config/topic-mv-registry.yaml)

RUN useradd -m -u 1000 app && chown -R app:app /app
USER app

EXPOSE 8091
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8091"]
