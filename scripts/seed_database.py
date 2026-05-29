"""
Seed the SQLite database from scraped JSON data files.

Usage:
    python -m scripts.seed_database [--languages am or ti en] [--force]

Flags:
    --languages   Space-separated list of language codes to seed (default: all)
    --force       Re-fetch source data even if cached
"""
import sys
import json
import asyncio
import argparse
from pathlib import Path

# Allow running from project root
sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.models import Base, Language, Book, BookName, Verse
from scripts.bible_books import BOOKS


DATA_DIR = Path(__file__).parent.parent / "data"

# Use app.config to build the DB URL — handles scheme conversion, SSL, and
# special characters in passwords consistently across app and scripts.
from app.config import settings

DB_URL = settings.get_database_url()

LANGUAGE_META = {
    "am":  ("Amharic",  "አማርኛ",         "ltr", DATA_DIR / "amharic.json"),
    "or":  ("Oromo",    "Afaan Oromoo",  "ltr", DATA_DIR / "oromo.json"),
    "ti":  ("Tigrigna", "ትግርኛ",          "ltr", DATA_DIR / "tigrigna.json"),
    "en":  ("English",  "English (KJV)", "ltr", DATA_DIR / "english.json"),
    "niv": ("English",  "English (NIV)", "ltr", DATA_DIR / "niv.json"),
}

# Index into BOOKS tuple for each language's localized name
LANG_NAME_IDX = {"am": 5, "or": 6, "ti": 7}


SCRAPE_CMDS = {
    "am":  "python -m scripts.scrape_amharic",
    "or":  "python -m scripts.scrape_oromo   (takes ~20 min — Bible.com)",
    "ti":  "python -m scripts.scrape_tigrigna  (takes ~7 min — API calls)",
    "en":  "python -m scripts.scrape_english",
    "niv": "python -m scripts.scrape_niv     (takes ~5 min — bolls.life API)",
}


async def seed(lang_codes: list[str], force_scrape: bool):
    DATA_DIR.mkdir(exist_ok=True)

    # --- fetch data if needed ---
    FETCHERS = {
        "am":  ("amharic.json",  "scripts.scrape_amharic",  "fetch_amharic"),
        "or":  ("oromo.json",    "scripts.scrape_oromo",    "fetch_oromo"),
        "ti":  ("tigrigna.json", "scripts.scrape_tigrigna", "fetch_tigrigna"),
        "en":  ("english.json",  "scripts.scrape_english",  "fetch_english"),
        "niv": ("niv.json",      "scripts.scrape_niv",      "fetch_niv"),
    }

    for code in lang_codes:
        if code not in FETCHERS:
            continue
        fname, module, func_name = FETCHERS[code]
        json_path = DATA_DIR / fname

        if json_path.exists() and not force_scrape:
            continue   # cached — skip

        if not force_scrape:
            print(
                f"\n[seed] '{code}' data not found.\n"
                f"  Run first:  {SCRAPE_CMDS.get(code, f'python -m scripts.scrape_{code}')}\n"
                f"  Then re-run: python -m scripts.seed_database --languages {code}\n"
            )
            sys.exit(1)

        # --force: delete cache and re-fetch
        if json_path.exists():
            json_path.unlink()
        import importlib
        mod = importlib.import_module(module)
        fn = getattr(mod, func_name)
        kwargs = {"force": True} if code in ("or", "ti") else {}
        fn(**kwargs)

    # --- database ---
    from app.database import _get_connect_args
    connect_args = _get_connect_args(DB_URL)
    engine = create_async_engine(DB_URL, echo=False, connect_args=connect_args)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with Session() as db:
        # SQLite-only performance pragmas
        if not is_postgres:
            await db.execute(text("PRAGMA journal_mode=WAL"))
            await db.execute(text("PRAGMA synchronous=NORMAL"))

        # --- seed books (once) ---
        existing = (await db.execute(text("SELECT COUNT(*) FROM books"))).scalar()
        if existing == 0:
            print("[seed] Inserting 66 books...")
            for row in BOOKS:
                num, abbr, eng_name, testament, chapters, *_names = row
                db.add(Book(
                    number=num,
                    english_name=eng_name,
                    abbreviation=abbr,
                    testament=testament,
                    chapter_count=chapters,
                ))
            await db.commit()

        # Fetch book id map: number → id
        from sqlalchemy import select
        books_result = await db.execute(select(Book))
        book_map = {b.number: b.id for b in books_result.scalars().all()}

        # --- seed each language ---
        for code in lang_codes:
            if code not in LANGUAGE_META:
                print(f"[seed] Unknown language code: {code}")
                continue

            lang_name, native_name, direction, json_path = LANGUAGE_META[code]

            if not json_path.exists():
                print(f"[seed] {json_path} not found — skipping {code}")
                continue

            # Upsert language row
            lang_result = await db.execute(
                select(Language).where(Language.code == code)
            )
            lang = lang_result.scalar_one_or_none()
            if not lang:
                lang = Language(
                    code=code,
                    name=lang_name,
                    native_name=native_name,
                    direction=direction,
                )
                db.add(lang)
                await db.flush()

            print(f"[seed] Language: {lang_name} ({code}), id={lang.id}")

            # Seed localized book names
            for row in BOOKS:
                num, *rest = row
                if code in LANG_NAME_IDX:
                    local_name = row[LANG_NAME_IDX[code]]
                else:
                    local_name = row[2]  # English name

                existing_name = (await db.execute(
                    select(BookName).where(
                        BookName.book_id == book_map[num],
                        BookName.language_id == lang.id,
                    )
                )).scalar_one_or_none()

                if not existing_name:
                    db.add(BookName(
                        book_id=book_map[num],
                        language_id=lang.id,
                        name=local_name,
                    ))

            await db.flush()

            # Seed verses
            print(f"[seed] Loading verses from {json_path}...")
            bible_data = json.loads(json_path.read_text(encoding="utf-8"))

            existing_verses = (await db.execute(
                text(f"SELECT COUNT(*) FROM verses WHERE language_id = {lang.id}")
            )).scalar()
            if existing_verses > 0:
                print(f"[seed] {code} already has {existing_verses} verses — skipping verse insert")
                continue

            batch = []
            total_verses = 0

            for book_data in bible_data["books"]:
                book_num = book_data["number"]
                if book_num not in book_map:
                    continue
                book_id = book_map[book_num]

                for ch_idx, chapter_verses in enumerate(book_data["chapters"], start=1):
                    for v_idx, verse_text in enumerate(chapter_verses, start=1):
                        verse_text = verse_text.strip()
                        if not verse_text:
                            continue
                        batch.append({
                            "book_id": book_id,
                            "language_id": lang.id,
                            "chapter": ch_idx,
                            "verse": v_idx,
                            "text": verse_text,
                        })
                        total_verses += 1

                        if len(batch) >= 2000:
                            await db.execute(
                                Verse.__table__.insert(),
                                batch,
                            )
                            batch = []
                            print(f"  ... {total_verses} verses inserted", end="\r")

            if batch:
                await db.execute(Verse.__table__.insert(), batch)

            await db.commit()
            print(f"[seed] {code}: {total_verses} verses committed")

    await engine.dispose()
    print("[seed] Done.")


def main():
    parser = argparse.ArgumentParser(description="Seed Ethiopian Bible database")
    parser.add_argument(
        "--languages", nargs="+", default=["am", "or", "ti", "en", "niv"],
        help="Language codes to seed (default: all)"
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Re-fetch source data even if cached"
    )
    args = parser.parse_args()
    asyncio.run(seed(args.languages, args.force))


if __name__ == "__main__":
    main()
