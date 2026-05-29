FROM python:3.13-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ app/
COPY scripts/ scripts/
COPY data/ data/
COPY start.sh .
RUN chmod +x start.sh

EXPOSE 8000

# Runtime env vars needed:
#   PORT            — set by Railway automatically
#   DATABASE_URL    — Supabase PostgreSQL connection string
#   MISTRAL_API_KEY — set in Railway dashboard → Variables for quiz generation

CMD ["./start.sh"]
