FROM python:3.13-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ app/
COPY scripts/ scripts/
COPY data/ data/

# Seed the database at build time from bundled JSON files
RUN python -m scripts.seed_database --languages am or ti en niv

EXPOSE 8000

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 4"]
