import re
import hashlib
from datetime import date
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from app.database import get_db
from app.models import Book, BookName, Language, Verse
from app.schemas import ChapterOut, VerseOut
from app.routers.books import _resolve_language, _resolve_book

router = APIRouter(tags=["Verses"])


async def _build_verse_out(v: Verse, book: Book, lang_code: str, book_name: str) -> VerseOut:
    return VerseOut(
        book=book.abbreviation,
        book_name=book_name,
        chapter=v.chapter,
        verse=v.verse,
        text=v.text,
        language=lang_code,
    )


async def _get_book_name(book_id: int, lang_id: int, english_name: str, db: AsyncSession) -> str:
    result = await db.execute(
        select(BookName.name).where(
            BookName.book_id == book_id,
            BookName.language_id == lang_id,
        )
    )
    return result.scalar_one_or_none() or english_name


@router.get("/{lang}/books/{book}/{chapter}", response_model=ChapterOut)
async def get_chapter(
    lang: str,
    book: str,
    chapter: int,
    db: AsyncSession = Depends(get_db),
):
    language = await _resolve_language(lang, db)
    b = await _resolve_book(book, db)

    if chapter < 1 or chapter > b.chapter_count:
        raise HTTPException(
            400, f"{b.english_name} only has {b.chapter_count} chapters"
        )

    result = await db.execute(
        select(Verse)
        .where(
            Verse.book_id == b.id,
            Verse.language_id == language.id,
            Verse.chapter == chapter,
        )
        .order_by(Verse.verse)
    )
    verses = result.scalars().all()
    if not verses:
        raise HTTPException(404, "No verses found — data may not be seeded for this language")

    book_name = await _get_book_name(b.id, language.id, b.english_name, db)
    verse_outs = [await _build_verse_out(v, b, language.code, book_name) for v in verses]

    return ChapterOut(
        book=b.abbreviation,
        book_name=book_name,
        chapter=chapter,
        verse_count=len(verse_outs),
        language=language.code,
        verses=verse_outs,
    )


@router.get("/{lang}/books/{book}/{chapter}/{verse_num}", response_model=VerseOut)
async def get_verse(
    lang: str,
    book: str,
    chapter: int,
    verse_num: int,
    db: AsyncSession = Depends(get_db),
):
    language = await _resolve_language(lang, db)
    b = await _resolve_book(book, db)

    result = await db.execute(
        select(Verse).where(
            Verse.book_id == b.id,
            Verse.language_id == language.id,
            Verse.chapter == chapter,
            Verse.verse == verse_num,
        )
    )
    v = result.scalar_one_or_none()
    if not v:
        raise HTTPException(404, f"{b.english_name} {chapter}:{verse_num} not found")

    book_name = await _get_book_name(b.id, language.id, b.english_name, db)
    return await _build_verse_out(v, b, language.code, book_name)


@router.get("/{lang}/random", response_model=VerseOut)
async def random_verse(lang: str, db: AsyncSession = Depends(get_db)):
    language = await _resolve_language(lang, db)

    result = await db.execute(
        select(Verse)
        .where(Verse.language_id == language.id)
        .order_by(func.random())
        .limit(1)
    )
    v = result.scalar_one_or_none()
    if not v:
        raise HTTPException(404, "No verses found — run seed_database.py first")

    book_result = await db.execute(select(Book).where(Book.id == v.book_id))
    b = book_result.scalar_one()
    book_name = await _get_book_name(b.id, language.id, b.english_name, db)
    return await _build_verse_out(v, b, language.code, book_name)


@router.get("/{lang}/passage", response_model=list[VerseOut])
async def get_by_reference(
    lang: str,
    ref: str = Query(..., description="e.g. John 3:16 or Genesis 1:1-5"),
    db: AsyncSession = Depends(get_db),
):
    """
    Parse human-readable references like 'John 3:16', 'Genesis 1:1-5',
    'Psalms 23', or 'Jn 3:16'.
    """
    language = await _resolve_language(lang, db)

    # Patterns: "Book Chapter" or "Book Chapter:Verse" or "Book Chapter:Start-End"
    m = re.match(
        r"^(.+?)\s+(\d+)(?::(\d+)(?:-(\d+))?)?$",
        ref.strip(),
        re.IGNORECASE,
    )
    if not m:
        raise HTTPException(400, "Invalid reference format. Use e.g. 'John 3:16' or 'Genesis 1:1-5'")

    book_str, chap_str, verse_start_str, verse_end_str = m.groups()
    chapter = int(chap_str)

    b = await _resolve_book(book_str.strip(), db)

    query = select(Verse).where(
        Verse.book_id == b.id,
        Verse.language_id == language.id,
        Verse.chapter == chapter,
    )

    if verse_start_str:
        v_start = int(verse_start_str)
        v_end = int(verse_end_str) if verse_end_str else v_start
        query = query.where(Verse.verse >= v_start, Verse.verse <= v_end)

    query = query.order_by(Verse.verse)
    result = await db.execute(query)
    verses = result.scalars().all()

    if not verses:
        raise HTTPException(404, f"No verses found for '{ref}'")

    book_name = await _get_book_name(b.id, language.id, b.english_name, db)
    return [await _build_verse_out(v, b, language.code, book_name) for v in verses]


@router.get("/{lang}/votd", response_model=VerseOut)
async def verse_of_the_day(lang: str, db: AsyncSession = Depends(get_db)):
    """
    Returns a deterministic Verse of the Day that changes daily.
    The same verse is returned for every request on the same calendar day.
    """
    language = await _resolve_language(lang, db)

    # Count total verses for this language
    count_result = await db.execute(
        select(func.count(Verse.id)).where(Verse.language_id == language.id)
    )
    total = count_result.scalar_one()
    if not total:
        raise HTTPException(404, "No verses found — run seed_database.py first")

    # Deterministic index based on today's date + language code
    seed_str = f"{date.today().isoformat()}-{lang}"
    idx = int(hashlib.md5(seed_str.encode()).hexdigest(), 16) % total

    result = await db.execute(
        select(Verse)
        .where(Verse.language_id == language.id)
        .offset(idx)
        .limit(1)
    )
    v = result.scalar_one_or_none()
    if not v:
        raise HTTPException(404, "Verse not found")

    book_result = await db.execute(select(Book).where(Book.id == v.book_id))
    b = book_result.scalar_one()
    book_name = await _get_book_name(b.id, language.id, b.english_name, db)
    return await _build_verse_out(v, b, language.code, book_name)


@router.get("/compare/{book}/{chapter}/{verse_num}", response_model=list[VerseOut])
async def compare_verse_all_languages(
    book: str,
    chapter: int,
    verse_num: int,
    db: AsyncSession = Depends(get_db),
):
    """
    Get the same verse in all available languages for side-by-side comparison.
    """
    b = await _resolve_book(book, db)

    verses_result = await db.execute(
        select(Verse, Language)
        .join(Language, Verse.language_id == Language.id)
        .where(
            Verse.book_id == b.id,
            Verse.chapter == chapter,
            Verse.verse == verse_num,
        )
        .order_by(Language.id)
    )
    rows = verses_result.all()

    if not rows:
        raise HTTPException(404, f"{b.english_name} {chapter}:{verse_num} not found in any language")

    out = []
    for v, lang in rows:
        book_name = await _get_book_name(b.id, lang.id, b.english_name, db)
        out.append(VerseOut(
            book=b.abbreviation,
            book_name=book_name,
            chapter=v.chapter,
            verse=v.verse,
            text=v.text,
            language=lang.code,
        ))
    return out
