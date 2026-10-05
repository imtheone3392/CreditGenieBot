FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["sh", "-c", "echo '=== DISK CHECK ===' && ls -ld /var /var/data || true && echo 'test' > /var/data/write-test.txt && echo 'DISK WRITE OK' && ls -la /var/data && echo 'DB_PATH='${DB_PATH} && exec uvicorn app:api --host 0.0.0.0 --port ${PORT:-8000}"]
