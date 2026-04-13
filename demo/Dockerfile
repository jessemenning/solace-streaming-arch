FROM python:3.11-slim

WORKDIR /app
COPY demo/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY demo/app.py demo/index.html ./
COPY backfill ./backfill

RUN useradd -m -u 1000 app && chown -R app:app /app
USER app

EXPOSE 8090
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8090"]
