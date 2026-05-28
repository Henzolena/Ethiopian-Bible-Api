from datetime import datetime
from pydantic import BaseModel, Field, field_validator
from typing import Optional, Literal


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


# ---------------------------------------------------------------------------
# Quiz / Trivia Schemas
# ---------------------------------------------------------------------------

class QuizOption(BaseModel):
    """One answer choice in a multiple-choice question."""
    label: str = Field(description="Option letter: A, B, C, or D")
    text: str  = Field(description="The answer choice text")


class QuizQuestionOut(BaseModel):
    """A single Bible trivia question with all answer choices."""
    id: int
    book: str           = Field(description="Book abbreviation e.g. GEN")
    book_name: str      = Field(description="Full English book name e.g. Genesis")
    chapter: int
    verse_start: Optional[int] = Field(None, description="Starting verse (null = whole chapter)")
    verse_end:   Optional[int] = Field(None, description="Ending verse (same as start for single verse)")
    verse_ref:   str           = Field(description="Human-readable ref e.g. Gen 1:1 or Gen 1:1-5")
    language:    str           = Field(description="Language/translation code e.g. niv")

    question:       str             = Field(description="The question text")
    options:        list[QuizOption] = Field(description="Four answer choices A–D")
    correct_answer: str             = Field(description="Correct option label: A, B, C, or D")
    explanation:    Optional[str]   = Field(None, description="Explanation of why the answer is correct")

    difficulty: str = Field(description="beginner / intermediate / advanced")
    source:     str = Field(description="static (curated) or ai_generated")
    author:     Optional[str] = Field(None, description="Question author if known")
    is_verified: bool = Field(description="True if answer has been human-verified")
    created_at:  Optional[datetime] = None

    model_config = {"from_attributes": True}


class QuizListOut(BaseModel):
    """Paginated list of quiz questions."""
    total: int
    page: int
    page_size: int
    book: str
    book_name: str
    chapter: int
    language: str
    questions: list[QuizQuestionOut]


class GenerateQuizRequest(BaseModel):
    """Request body for AI-powered question generation."""
    book: str    = Field(description="Book abbreviation e.g. GEN, JHN, REV")
    chapter: int = Field(ge=1, description="Chapter number")
    verse_start: Optional[int] = Field(None, ge=1, description="Start verse (omit for whole chapter)")
    verse_end:   Optional[int] = Field(None, ge=1, description="End verse (defaults to verse_start)")
    count:       int = Field(5, ge=1, le=10, description="Number of questions to generate (1–10)")
    difficulty:  Optional[Literal["beginner", "intermediate", "advanced", "mixed"]] = Field(
        "mixed", description="Difficulty level; 'mixed' = variety of all three"
    )
    language: str = Field("niv", description="Bible translation to base questions on")
    save:     bool = Field(False, description="If true, persist generated questions to the database")

    @field_validator("book")
    @classmethod
    def upper_book(cls, v: str) -> str:
        return v.upper()


class GenerateQuizResponse(BaseModel):
    """Response from AI question generation."""
    book: str
    book_name: str
    chapter: int
    verse_start: Optional[int]
    verse_end:   Optional[int]
    verse_ref:   str
    language: str
    generated: int
    saved: bool
    questions: list[QuizQuestionOut]


class QuizAnswerSubmit(BaseModel):
    """Client submits an answer; API returns correctness + explanation."""
    question_id: int
    selected:    str = Field(description="Option chosen by user: A, B, C, or D")

    @field_validator("selected")
    @classmethod
    def upper_selected(cls, v: str) -> str:
        v = v.upper()
        if v not in ("A", "B", "C", "D"):
            raise ValueError("selected must be A, B, C, or D")
        return v


class QuizAnswerResult(BaseModel):
    """Result of answering a quiz question."""
    question_id:    int
    selected:       str
    correct_answer: str
    is_correct:     bool
    explanation:    Optional[str]
    verse_ref:      str
    book:           str
    chapter:        int
