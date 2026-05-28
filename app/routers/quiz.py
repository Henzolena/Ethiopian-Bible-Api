"""
Quiz / Trivia router.

Endpoints:
  GET  /api/v1/quiz/{lang}/books/{book}/{chapter}
       → Paginated list of stored questions for a chapter

  GET  /api/v1/quiz/{lang}/books/{book}/{chapter}/{verse}
       → Questions that reference a specific verse

  GET  /api/v1/quiz/random
       → Random questions (filterable by lang, book, difficulty)

  POST /api/v1/quiz/generate
       → AI-generate 1–10 questions via Gemini for any verse/chapter
         (optionally save to DB for future reuse)

  POST /api/v1/quiz/answer
       → Submit an answer; get correctness + explanation back

  GET  /api/v1/quiz/stats
       → Coverage stats — how many questions per book/lang
"""

import json
import re
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import Book, QuizQuestion, Verse, Language
from app.schemas import (
    GenerateQuizRequest,
    GenerateQuizResponse,
    QuizAnswerResult,
    QuizAnswerSubmit,
    QuizListOut,
    QuizOption,
    QuizQuestionOut,
)

router = APIRouter(prefix="/quiz", tags=["Quiz & Trivia"])

DIFFICULTY_ORDER = ["beginner", "intermediate", "advanced"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _verse_ref(book_abbr: str, chapter: int, vs: Optional[int], ve: Optional[int]) -> str:
    if vs is None:
        return f"{book_abbr} {chapter}"
    if ve and ve != vs:
        return f"{book_abbr} {chapter}:{vs}-{ve}"
    return f"{book_abbr} {chapter}:{vs}"


def _to_out(q: QuizQuestion, book: Book) -> QuizQuestionOut:
    return QuizQuestionOut(
        id=q.id,
        book=book.abbreviation,
        book_name=book.english_name,
        chapter=q.chapter,
        verse_start=q.verse_start,
        verse_end=q.verse_end,
        verse_ref=_verse_ref(book.abbreviation, q.chapter, q.verse_start, q.verse_end),
        language=q.language_code,
        question=q.question,
        options=[
            QuizOption(label="A", text=q.option_a),
            QuizOption(label="B", text=q.option_b),
            QuizOption(label="C", text=q.option_c),
            QuizOption(label="D", text=q.option_d),
        ],
        correct_answer=q.correct_answer,
        explanation=q.explanation,
        difficulty=q.difficulty,
        source=q.source,
        author=q.author,
        is_verified=q.is_verified,
        created_at=q.created_at,
    )


async def _resolve_book(book_str: str, db: AsyncSession) -> Book:
    """Resolve book by abbreviation or number."""
    if book_str.isdigit():
        result = await db.execute(select(Book).where(Book.number == int(book_str)))
    else:
        result = await db.execute(select(Book).where(Book.abbreviation == book_str.upper()))
    b = result.scalar_one_or_none()
    if not b:
        raise HTTPException(404, f"Book '{book_str}' not found")
    return b


# ---------------------------------------------------------------------------
# Gemini AI — generate questions
# ---------------------------------------------------------------------------

_GEMINI_SYSTEM = """\
You are a Bible quiz question writer. Given Bible verse text you will generate
multiple-choice trivia questions suitable for a Bible study app.

RULES:
- Each question must have exactly 4 options: A, B, C, D
- Exactly one option must be correct
- Wrong options (distractors) must be plausible but clearly wrong based on the text
- Do NOT include the answer letter in the question text
- Include a brief explanation (1-2 sentences) of why the answer is correct
- Vary difficulty if asked for mixed
- Return ONLY valid JSON — no markdown fences, no commentary
- difficulty values: "beginner", "intermediate", "advanced"

OUTPUT FORMAT (JSON array):
[
  {
    "question": "...",
    "option_a": "...",
    "option_b": "...",
    "option_c": "...",
    "option_d": "...",
    "correct_answer": "A",
    "explanation": "...",
    "difficulty": "beginner",
    "verse_start": 1,
    "verse_end": 1
  }
]
"""


async def _call_gemini(prompt: str) -> list[dict]:
    """Call Gemini API and return parsed JSON list of question dicts."""
    if not settings.gemini_api_key:
        raise HTTPException(503, "GEMINI_API_KEY not configured — set it in .env")

    url = f"{settings.gemini_base_url}/{settings.gemini_model}:generateContent?key={settings.gemini_api_key}"

    payload = {
        "system_instruction": {"parts": [{"text": _GEMINI_SYSTEM}]},
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.7,
            "maxOutputTokens": 4096,
            "responseMimeType": "application/json",
        },
    }

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(url, json=payload)
        if resp.status_code != 200:
            raise HTTPException(502, f"Gemini API error {resp.status_code}: {resp.text[:300]}")

    data = resp.json()
    try:
        raw_text = data["candidates"][0]["content"]["parts"][0]["text"]
        # Strip markdown code fences if Gemini wraps the JSON anyway
        raw_text = re.sub(r"^```(?:json)?\s*", "", raw_text.strip())
        raw_text = re.sub(r"\s*```$", "", raw_text.strip())
        return json.loads(raw_text)
    except (KeyError, IndexError, json.JSONDecodeError) as e:
        raise HTTPException(502, f"Failed to parse Gemini response: {e}\nRaw: {data!r}"[:500])


def _build_generation_prompt(
    book_name: str,
    chapter: int,
    verse_start: Optional[int],
    verse_end: Optional[int],
    verses_text: list[dict],
    count: int,
    difficulty: str,
    language: str,
) -> str:
    ref = f"{book_name} {chapter}"
    if verse_start:
        ref += f":{verse_start}" + (f"–{verse_end}" if verse_end and verse_end != verse_start else "")

    diff_instruction = (
        f"All questions should be {difficulty} difficulty."
        if difficulty != "mixed"
        else "Mix difficulty levels: some beginner, some intermediate, some advanced."
    )

    verses_block = "\n".join(
        f"  v{v['verse']}: {v['text']}" for v in verses_text
    )

    return (
        f"Bible passage: {ref} ({language.upper()})\n\n"
        f"{verses_block}\n\n"
        f"Generate exactly {count} multiple-choice questions about this passage.\n"
        f"{diff_instruction}\n"
        f"Set verse_start and verse_end fields to the verse number(s) your question is based on.\n"
        f"If the question covers the whole passage set verse_start=null and verse_end=null.\n"
        f"Return a JSON array of {count} question objects."
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/{lang}/books/{book}/{chapter}", response_model=QuizListOut)
async def get_chapter_questions(
    lang: str,
    book: str,
    chapter: int,
    difficulty: Optional[str] = Query(None, description="beginner / intermediate / advanced"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    """Get stored quiz questions for a specific chapter."""
    b = await _resolve_book(book, db)
    lang = lang.lower()

    query = (
        select(QuizQuestion)
        .where(
            QuizQuestion.book_id == b.id,
            QuizQuestion.language_code == lang,
            QuizQuestion.chapter == chapter,
        )
        .order_by(QuizQuestion.verse_start.nullsfirst(), QuizQuestion.id)
    )
    if difficulty:
        query = query.where(QuizQuestion.difficulty == difficulty.lower())

    total = (await db.execute(
        select(func.count()).select_from(query.subquery())
    )).scalar_one()

    paged = query.offset((page - 1) * page_size).limit(page_size)
    rows = (await db.execute(paged)).scalars().all()

    return QuizListOut(
        total=total,
        page=page,
        page_size=page_size,
        book=b.abbreviation,
        book_name=b.english_name,
        chapter=chapter,
        language=lang,
        questions=[_to_out(q, b) for q in rows],
    )


@router.get("/{lang}/books/{book}/{chapter}/{verse}", response_model=QuizListOut)
async def get_verse_questions(
    lang: str,
    book: str,
    chapter: int,
    verse: int,
    difficulty: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """Get stored questions that reference a specific verse."""
    b = await _resolve_book(book, db)
    lang = lang.lower()

    query = (
        select(QuizQuestion)
        .where(
            QuizQuestion.book_id == b.id,
            QuizQuestion.language_code == lang,
            QuizQuestion.chapter == chapter,
            QuizQuestion.verse_start <= verse,
            QuizQuestion.verse_end >= verse,
        )
        .order_by(QuizQuestion.verse_start, QuizQuestion.id)
    )
    if difficulty:
        query = query.where(QuizQuestion.difficulty == difficulty.lower())

    rows = (await db.execute(query)).scalars().all()

    return QuizListOut(
        total=len(rows),
        page=1,
        page_size=len(rows) or 1,
        book=b.abbreviation,
        book_name=b.english_name,
        chapter=chapter,
        language=lang,
        questions=[_to_out(q, b) for q in rows],
    )


@router.get("/random", response_model=list[QuizQuestionOut])
async def get_random_questions(
    lang: str = Query("niv", description="Language/translation code"),
    book: Optional[str] = Query(None, description="Filter by book abbreviation e.g. GEN"),
    difficulty: Optional[str] = Query(None, description="beginner / intermediate / advanced"),
    count: int = Query(5, ge=1, le=20, description="Number of random questions"),
    db: AsyncSession = Depends(get_db),
):
    """Get random quiz questions, optionally filtered."""
    query = select(QuizQuestion).where(QuizQuestion.language_code == lang.lower())

    if book:
        b = await _resolve_book(book, db)
        query = query.where(QuizQuestion.book_id == b.id)
    if difficulty:
        query = query.where(QuizQuestion.difficulty == difficulty.lower())

    query = query.order_by(func.random()).limit(count)
    rows = (await db.execute(query)).scalars().all()

    if not rows:
        raise HTTPException(404, "No questions found for the given filters")

    # Fetch books for each question
    book_ids = list({q.book_id for q in rows})
    books_result = await db.execute(select(Book).where(Book.id.in_(book_ids)))
    book_map = {b.id: b for b in books_result.scalars().all()}

    return [_to_out(q, book_map[q.book_id]) for q in rows]


@router.post("/generate", response_model=GenerateQuizResponse)
async def generate_questions(
    req: GenerateQuizRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    AI-generate 1–10 quiz questions via Gemini for any Bible verse or chapter.

    - Fetches the actual verse text from the database for the given language
    - Sends it to Gemini with a structured prompt
    - Returns fully-formed quiz questions with options, answers, and explanations
    - Set `save: true` to persist generated questions for future reuse
    """
    b = await _resolve_book(req.book, db)
    lang = req.language.lower()

    if req.chapter < 1 or req.chapter > b.chapter_count:
        raise HTTPException(400, f"{b.english_name} only has {b.chapter_count} chapters")

    # Fetch verse texts from DB
    verse_query = (
        select(Verse)
        .join(Language, Verse.language_id == Language.id)
        .where(
            Verse.book_id == b.id,
            Language.code == lang,
            Verse.chapter == req.chapter,
        )
        .order_by(Verse.verse)
    )
    if req.verse_start:
        v_end = req.verse_end or req.verse_start
        verse_query = verse_query.where(
            Verse.verse >= req.verse_start,
            Verse.verse <= v_end,
        )

    verse_rows = (await db.execute(verse_query)).scalars().all()
    if not verse_rows:
        raise HTTPException(
            404,
            f"No {lang.upper()} text found for {b.english_name} {req.chapter}"
            + (f":{req.verse_start}" if req.verse_start else "")
            + f". Make sure the language is seeded."
        )

    verses_text = [{"verse": v.verse, "text": v.text} for v in verse_rows]

    # Build prompt and call Gemini
    prompt = _build_generation_prompt(
        book_name=b.english_name,
        chapter=req.chapter,
        verse_start=req.verse_start,
        verse_end=req.verse_end or req.verse_start,
        verses_text=verses_text,
        count=req.count,
        difficulty=req.difficulty or "mixed",
        language=lang,
    )

    raw_questions = await _call_gemini(prompt)

    # Convert to QuizQuestion objects
    saved = False
    out_questions: list[QuizQuestionOut] = []
    db_questions: list[QuizQuestion] = []

    for i, rq in enumerate(raw_questions[: req.count]):
        vs = rq.get("verse_start") or req.verse_start
        ve = rq.get("verse_end") or req.verse_end or vs

        qq = QuizQuestion(
            id=-(i + 1),  # placeholder — will be replaced if saved
            book_id=b.id,
            language_code=lang,
            chapter=req.chapter,
            verse_start=vs,
            verse_end=ve,
            question=rq.get("question", ""),
            option_a=rq.get("option_a", ""),
            option_b=rq.get("option_b", ""),
            option_c=rq.get("option_c", ""),
            option_d=rq.get("option_d", ""),
            correct_answer=rq.get("correct_answer", "A").upper(),
            explanation=rq.get("explanation"),
            difficulty=rq.get("difficulty", "beginner"),
            source="ai_generated",
            author="Gemini AI",
            is_verified=False,
        )
        db_questions.append(qq)

    if req.save:
        for qq in db_questions:
            qq.id = None  # let DB auto-assign
        db.add_all(db_questions)
        await db.flush()
        await db.commit()
        saved = True

    # Assign placeholder IDs for unsaved questions
    for idx, qq in enumerate(db_questions):
        if qq.id is None or qq.id < 0:
            qq.id = -(idx + 1)
        out_questions.append(_to_out(qq, b))

    verse_ref = _verse_ref(b.abbreviation, req.chapter, req.verse_start, req.verse_end)

    return GenerateQuizResponse(
        book=b.abbreviation,
        book_name=b.english_name,
        chapter=req.chapter,
        verse_start=req.verse_start,
        verse_end=req.verse_end,
        verse_ref=verse_ref,
        language=lang,
        generated=len(out_questions),
        saved=saved,
        questions=out_questions,
    )


@router.post("/answer", response_model=QuizAnswerResult)
async def submit_answer(
    body: QuizAnswerSubmit,
    db: AsyncSession = Depends(get_db),
):
    """
    Submit an answer to a stored question.
    Returns whether it's correct and the explanation.
    """
    result = await db.execute(
        select(QuizQuestion).where(QuizQuestion.id == body.question_id)
    )
    qq = result.scalar_one_or_none()
    if not qq:
        raise HTTPException(404, f"Question {body.question_id} not found")

    book_result = await db.execute(select(Book).where(Book.id == qq.book_id))
    b = book_result.scalar_one()

    return QuizAnswerResult(
        question_id=qq.id,
        selected=body.selected,
        correct_answer=qq.correct_answer,
        is_correct=body.selected == qq.correct_answer,
        explanation=qq.explanation,
        verse_ref=_verse_ref(b.abbreviation, qq.chapter, qq.verse_start, qq.verse_end),
        book=b.abbreviation,
        chapter=qq.chapter,
    )


@router.get("/stats")
async def quiz_stats(db: AsyncSession = Depends(get_db)):
    """
    Returns a breakdown of how many questions are stored,
    grouped by book, language, difficulty, and source.
    """
    # Total
    total = (await db.execute(select(func.count(QuizQuestion.id)))).scalar_one()

    # By book
    by_book_rows = (await db.execute(
        select(Book.abbreviation, Book.english_name, func.count(QuizQuestion.id))
        .join(QuizQuestion, QuizQuestion.book_id == Book.id)
        .group_by(Book.id)
        .order_by(Book.number)
    )).all()

    # By language
    by_lang_rows = (await db.execute(
        select(QuizQuestion.language_code, func.count(QuizQuestion.id))
        .group_by(QuizQuestion.language_code)
    )).all()

    # By difficulty
    by_diff_rows = (await db.execute(
        select(QuizQuestion.difficulty, func.count(QuizQuestion.id))
        .group_by(QuizQuestion.difficulty)
    )).all()

    # By source
    by_source_rows = (await db.execute(
        select(QuizQuestion.source, func.count(QuizQuestion.id))
        .group_by(QuizQuestion.source)
    )).all()

    return {
        "total_questions": total,
        "by_book": [
            {"abbreviation": abbr, "name": name, "count": cnt}
            for abbr, name, cnt in by_book_rows
        ],
        "by_language": {lang: cnt for lang, cnt in by_lang_rows},
        "by_difficulty": {diff: cnt for diff, cnt in by_diff_rows},
        "by_source": {src: cnt for src, cnt in by_source_rows},
    }
