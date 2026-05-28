FROM python:3.13-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ app/
COPY scripts/ scripts/
COPY data/ data/

# Seed the database at build time from bundled JSON files
RUN python -m scripts.seed_database --languages am or ti en niv

# Seed Genesis quiz questions from bundled PDF
RUN python -m scripts.parse_genesis_questions data/genesis_questions.pdf

EXPOSE 8000

# Runtime env vars needed:
#   PORT            — set by Railway automatically
#   GEMINI_API_KEY  — set in Railway dashboard → Variables for quiz generation

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 4"]
