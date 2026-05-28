from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.database import get_db
from app.models import Language
from app.schemas import LanguageOut

router = APIRouter(prefix="/languages", tags=["Languages"])


@router.get("", response_model=list[LanguageOut])
async def list_languages(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Language).order_by(Language.id))
    return result.scalars().all()


@router.get("/{code}", response_model=LanguageOut)
async def get_language(code: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Language).where(Language.code == code.lower())
    )
    lang = result.scalar_one_or_none()
    if not lang:
        raise HTTPException(404, f"Language '{code}' not found")
    return lang
