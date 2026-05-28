.PHONY: install seed seed-am seed-en seed-ti seed-or dev scrape-all clean

install:
	pip install -r requirements.txt

# Seed specific languages (skip scraping if data already cached)
seed-am:
	python -m scripts.seed_database --languages am

seed-en:
	python -m scripts.seed_database --languages en

seed-ti:
	python -m scripts.seed_database --languages ti

seed-or:
	python -m scripts.seed_database --languages or

# Seed all languages
seed:
	python -m scripts.seed_database --languages am or ti en

# Scrape fresh data (deletes cache, re-downloads everything)
scrape-all:
	python -m scripts.scrape_amharic
	python -m scripts.scrape_english
	python -m scripts.scrape_oromo --force
	python -m scripts.scrape_tigrigna --force

# Start the development server
dev:
	uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# Start production server
start:
	uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 4

clean:
	rm -f data/bible.db
	rm -f data/*.json
