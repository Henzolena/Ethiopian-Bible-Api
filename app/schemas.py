from pydantic import BaseModel, Field
from typing import Optional


class LanguageOut(BaseModel):
    code: str
    name: str
    native_name: str
    direction: str

    model_config = {"from_attributes": True}


class BookOut(BaseModel):
    number: int
    english_name: str
    abbreviation: str
    testament: str
    chapter_count: int
    name: Optional[str] = None   # localized name, populated by router

    model_config = {"from_attributes": True}


class VerseOut(BaseModel):
    book: str = Field(description="Book abbreviation e.g. GEN")
    book_name: str
    chapter: int
    verse: int
    text: str
    language: str

    model_config = {"from_attributes": True}


class ChapterOut(BaseModel):
    book: str
    book_name: str
    chapter: int
    verse_count: int
    language: str
    verses: list[VerseOut]


class SearchResult(BaseModel):
    total: int
    page: int
    page_size: int
    results: list[VerseOut]


class APIInfo(BaseModel):
    title: str
    version: str
    languages: list[str]
    endpoints: dict
