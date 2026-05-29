#!/bin/sh

echo "[start] Seeding database..."
python -m scripts.seed_database --languages am or ti en niv || echo "[start] Seeder failed or already seeded — continuing"

echo "[start] Seeding Genesis quiz questions..."
python -m scripts.parse_genesis_questions data/genesis_questions.pdf || echo "[start] Genesis quiz seed failed or already seeded — continuing"

echo "[start] Starting API server..."
exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 4
