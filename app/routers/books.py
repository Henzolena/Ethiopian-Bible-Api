import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.database import get_db
from app.models import Book, BookName, Language
from app.schemas import BookOut

try:
    from scripts.bible_books import BOOK_ALIASES
except ImportError:
    BOOK_ALIASES = {}

router = APIRouter(tags=["Books"])


async def _resolve_language(code: str, db: AsyncSession) -> Language:
    result = await db.execute(
        select(Language).where(Language.code == code.lower())
    )
    lang = result.scalar_one_or_none()
    if not lang:
        raise HTTPException(404, f"Language '{code}' not found. Available: am, or, ti, en")
    return lang


async def _resolve_book(identifier: str, db: AsyncSession) -> Book:
    """Accept book number (1-66), abbreviation (GEN, EXO…), or common name (john, genesis…)."""
    if identifier.isdigit():
        result = await db.execute(
            select(Book).where(Book.number == int(identifier))
        )
    else:
        # Check alias map first (handles 'john', 'jn', 'genesis', '1cor', etc.)
        abbr = BOOK_ALIASES.get(identifier.lower(), identifier.upper())
        result = await db.execute(
            select(Book).where(Book.abbreviation == abbr)
        )
    book = result.scalar_one_or_none()
    if not book:
        raise HTTPException(404, f"Book '{identifier}' not found. Use a number (1-66), abbreviation (GEN), or English name (genesis)")
    return book


async def _localized_name(book_id: int, lang_id: int, db: AsyncSession) -> str:
    result = await db.execute(
        select(BookName.name).where(
            BookName.book_id == book_id,
            BookName.language_id == lang_id,
        )
    )
    row = result.scalar_one_or_none()
    return row or ""


@router.get("/{lang}/books", response_model=list[BookOut])
async def list_books(lang: str, db: AsyncSession = Depends(get_db)):
    language = await _resolve_language(lang, db)

    books_result = await db.execute(select(Book).order_by(Book.number))
    books = books_result.scalars().all()

    names_result = await db.execute(
        select(BookName).where(BookName.language_id == language.id)
    )
    name_map = {bn.book_id: bn.name for bn in names_result.scalars().all()}

    out = []
    for b in books:
        out.append(BookOut(
            number=b.number,
            english_name=b.english_name,
            abbreviation=b.abbreviation,
            testament=b.testament,
            chapter_count=b.chapter_count,
            name=name_map.get(b.id, b.english_name),
        ))
    return out


@router.get("/{lang}/books/{book}", response_model=BookOut)
async def get_book(lang: str, book: str, db: AsyncSession = Depends(get_db)):
    language = await _resolve_language(lang, db)
    b = await _resolve_book(book, db)
    name = await _localized_name(b.id, language.id, db)
    return BookOut(
        number=b.number,
        english_name=b.english_name,
        abbreviation=b.abbreviation,
        testament=b.testament,
        chapter_count=b.chapter_count,
        name=name or b.english_name,
    )


@router.get("/{lang}/testament/{testament}", response_model=list[BookOut])
async def books_by_testament(
    lang: str,
    testament: str,
    db: AsyncSession = Depends(get_db),
):
    """Get all books for a given testament. testament = OT or NT."""
    testament = testament.upper()
    if testament not in ("OT", "NT"):
        raise HTTPException(400, "testament must be OT or NT")

    language = await _resolve_language(lang, db)

    books_result = await db.execute(
        select(Book).where(Book.testament == testament).order_by(Book.number)
    )
    books = books_result.scalars().all()

    names_result = await db.execute(
        select(BookName).where(BookName.language_id == language.id)
    )
    name_map = {bn.book_id: bn.name for bn in names_result.scalars().all()}

    return [
        BookOut(
            number=b.number,
            english_name=b.english_name,
            abbreviation=b.abbreviation,
            testament=b.testament,
            chapter_count=b.chapter_count,
            name=name_map.get(b.id, b.english_name),
        )
        for b in books
    ]
