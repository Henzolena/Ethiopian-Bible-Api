"""
Delete all existing Amharic verses from the database and re-seed from data/amharic.json.

Run this AFTER deploying the new amharic.json to Railway:
    railway run python -m scripts.replace_amharic_verses

Safe to run multiple times — always starts fresh for Amharic only, leaves other languages untouched.
"""
import json
import sys
import asyncio
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import text, select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.config import settings
from app.models import Language, Book, Verse

DATA_FILE = Path(__file__).parent.parent / "data" / "amharic.json"


async def replace_amharic():
    if not DATA_FILE.exists():
        print(f"[replace] ERROR: {DATA_FILE} not found — run scraper first")
        sys.exit(1)

    db_url = settings.get_database_url()
    print(f"[replace] Connecting to: {db_url[:40]}...")

    from app.database import _get_connect_args
    connect_args = _get_connect_args(db_url)
    engine = create_async_engine(db_url, echo=False, connect_args=connect_args)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async with Session() as db:
        # Find Amharic language record
        lang_result = await db.execute(
            select(Language).where(Language.code == "am")
        )
        lang = lang_result.scalar_one_or_none()
        if not lang:
            print("[replace] ERROR: Language 'am' not found in DB — run seed_database first")
            await engine.dispose()
            sys.exit(1)

        # Count existing
        old_count = (await db.execute(
            text(f"SELECT COUNT(*) FROM verses WHERE language_id = {lang.id}")
        )).scalar()
        print(f"[replace] Deleting {old_count} existing Amharic verses (lang_id={lang.id})...")

        await db.execute(
            text(f"DELETE FROM verses WHERE language_id = {lang.id}")
        )
        await db.commit()
        print("[replace] Deleted.")

        # Build book number → id map
        books_result = await db.execute(select(Book))
        book_map = {b.number: b.id for b in books_result.scalars().all()}

        # Load new data
        print(f"[replace] Loading {DATA_FILE}...")
        bible_data = json.loads(DATA_FILE.read_text(encoding="utf-8"))

        batch = []
        total = 0

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
                        "book_id":     book_id,
                        "language_id": lang.id,
                        "chapter":     ch_idx,
                        "verse":       v_idx,
                        "text":        verse_text,
                    })
                    total += 1

                    if len(batch) >= 2000:
                        await db.execute(Verse.__table__.insert(), batch)
                        batch = []
                        print(f"  ... {total} verses inserted", end="\r")

        if batch:
            await db.execute(Verse.__table__.insert(), batch)

        await db.commit()
        print(f"\n[replace] Done! {total} Amharic verses inserted.")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(replace_amharic())
