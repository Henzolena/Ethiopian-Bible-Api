.PHONY: install seed seed-am seed-en seed-ti seed-or seed-fonts dev scrape-all mezmur-crawl-test mezmur-crawl mezmur-normalize mezmur-validate test-gemini-keys clean

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

seed-fonts:
	python -m scripts.seed_fonts

# Scrape fresh data (deletes cache, re-downloads everything)
scrape-all:
	python -m scripts.scrape_amharic
	python -m scripts.scrape_english
	python -m scripts.scrape_oromo --force
	python -m scripts.scrape_tigrigna --force

mezmur-crawl-test:
	python -m scripts.run_mezmur_crawler --fresh --limit 25 --spiders mezmuroch
	python -m scripts.normalize_mezmur_sources --output data/mezmur_data.test.json --min-lyrics 1
	python -m scripts.seed_mezmur --data-file data/mezmur_data.test.json --validate-only
	rm -f data/mezmur_data.test.json

mezmur-crawl:
	python -m scripts.run_mezmur_crawler --fresh --spiders mezmuroch,wikimezmur
	python -m scripts.normalize_mezmur_sources --min-lyrics 1

mezmur-normalize:
	python -m scripts.normalize_mezmur_sources --min-lyrics 1

mezmur-validate:
	python -m scripts.seed_mezmur --validate-only

# Start the development server
dev:
	uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# Start production server
start:
	uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 4

test-gemini-keys:
	python -m scripts.test_gemini_keys

clean:
	rm -f data/bible.db
	rm -f data/*.json
