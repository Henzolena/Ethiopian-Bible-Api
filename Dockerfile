FROM python:3.13-slim

WORKDIR /app

# ffmpeg required by pydub for WAV → MP3 conversion (VOTD audio pipeline)
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

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
