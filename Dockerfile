FROM python:3.13-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ app/
COPY scripts/ scripts/
COPY data/bible.db data/bible.db

EXPOSE 8000

CMD ["sh", "-c", "echo '=== DB CHECK ===' && ls -lh data/ && python3 -c \"import sqlite3; c=sqlite3.connect('data/bible.db'); print('verses:', c.execute('SELECT COUNT(*) FROM verses').fetchone()[0]); print('langs:', c.execute('SELECT COUNT(*) FROM languages').fetchone()[0])\" && uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 4"]
