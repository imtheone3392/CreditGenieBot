FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["sh", "-c", "mkdir -p /var/data && chmod 755 /var/data && exec uvicorn app:api --host 0.0.0.0 --port ${PORT:-8000}"]
