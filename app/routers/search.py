from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from app.database import get_db
from app.models import Book, BookName, Language, Verse
from app.schemas import SearchResult, VerseOut
from app.routers.books import _resolve_language

router = APIRouter(tags=["Search"])


@router.get("/{lang}/search", response_model=SearchResult)
async def search_verses(
    lang: str,
    q: str = Query(..., min_length=2, description="Search term"),
    testament: str = Query(None, description="Filter: OT or NT"),
    book: str = Query(None, description="Filter by book abbreviation, e.g. GEN"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    language = await _resolve_language(lang, db)

    query = (
        select(Verse)
        .join(Book, Verse.book_id == Book.id)
        .where(
            Verse.language_id == language.id,
            Verse.text.like(f"%{q}%"),
        )
    )

    if testament:
        query = query.where(Book.testament == testament.upper())

    if book:
        query = query.where(Book.abbreviation == book.upper())

    count_q = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_q)).scalar_one()

    query = query.order_by(Book.number, Verse.chapter, Verse.verse)
    query = query.offset((page - 1) * page_size).limit(page_size)

    result = await db.execute(query)
    verses = result.scalars().all()

    book_ids = list({v.book_id for v in verses})
    books_result = await db.execute(select(Book).where(Book.id.in_(book_ids)))
    book_map = {b.id: b for b in books_result.scalars().all()}

    names_result = await db.execute(
        select(BookName).where(
            BookName.language_id == language.id,
            BookName.book_id.in_(book_ids),
        )
    )
    name_map = {bn.book_id: bn.name for bn in names_result.scalars().all()}

    verse_outs = []
    for v in verses:
        b = book_map[v.book_id]
        verse_outs.append(VerseOut(
            book=b.abbreviation,
            book_name=name_map.get(b.id, b.english_name),
            chapter=v.chapter,
            verse=v.verse,
            text=v.text,
            language=language.code,
        ))

    return SearchResult(
        total=total,
        page=page,
        page_size=page_size,
        results=verse_outs,
    )
