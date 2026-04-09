FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy server and UI
COPY server.py index.html ./

# Registry is mounted from project root at runtime via volume
# (config/topic-mv-registry.yaml → /app/config/topic-mv-registry.yaml)

EXPOSE 8091
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8091"]
