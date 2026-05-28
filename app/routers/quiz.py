"""
Quiz / Trivia router.

NOTE: This router MUST be registered in main.py BEFORE generic /{lang}/...
      routers (verses, books, search, audio). FastAPI is first-match — the
      /quiz prefix must win over the wildcard /{lang} pattern.

Endpoints:
  GET  /api/v1/quiz/stats
  GET  /api/v1/quiz/random
  POST /api/v1/quiz/generate
  POST /api/v1/quiz/answer
  GET  /api/v1/quiz/{lang}/books/{book}/{chapter}
  GET  /api/v1/quiz/{lang}/books/{book}/{chapter}/{verse}
"""

import json
import re
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import Book, Language, QuizQuestion, Verse
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _quiz_error(status: int, code: str, message: str, hint: str = None) -> HTTPException:
    """
    Raise a structured quiz error that clients can handle by error_code.
    Shape: { "error": true, "error_code": "...", "message": "...", "hint": "..." }
    """
    detail = {"error": True, "error_code": code, "message": message}
    if hint:
        detail["hint"] = hint
    return HTTPException(status_code=status, detail=detail)


def _verse_ref(abbr: str, chapter: int, vs: Optional[int], ve: Optional[int]) -> str:
    if vs is None:
        return f"{abbr} {chapter}"
    if ve and ve != vs:
        return f"{abbr} {chapter}:{vs}-{ve}"
    return f"{abbr} {chapter}:{vs}"


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
    if book_str.isdigit():
        result = await db.execute(select(Book).where(Book.number == int(book_str)))
    else:
        result = await db.execute(select(Book).where(Book.abbreviation == book_str.upper()))
    b = result.scalar_one_or_none()
    if not b:
        raise _quiz_error(
            404, "BOOK_NOT_FOUND",
            f"Book '{book_str}' not found.",
            "Use a 3-letter abbreviation (GEN, JHN, REV) or a book number (1–66).",
        )
    return b


async def _resolve_lang(lang: str, db: AsyncSession) -> str:
    """Validate language code exists in DB. Returns normalised lowercase code."""
    code = lang.lower()
    result = await db.execute(select(Language).where(Language.code == code))
    if not result.scalar_one_or_none():
        all_langs = (await db.execute(select(Language.code))).scalars().all()
        raise _quiz_error(
            404, "LANGUAGE_NOT_FOUND",
            f"Language '{code}' not found.",
            f"Available language codes: {sorted(all_langs)}",
        )
    return code


# ---------------------------------------------------------------------------
# Gemini AI — question generation
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

# Gemini API error code → client-friendly message
_GEMINI_ERROR_MESSAGES = {
    400: ("GEMINI_BAD_REQUEST",   "The AI request was malformed.", None),
    401: ("GEMINI_UNAUTHORIZED",  "Gemini API key is invalid or missing.",
          "Set a valid GEMINI_API_KEY in your environment variables."),
    403: ("GEMINI_FORBIDDEN",     "Gemini API key does not have permission.",
          "Check your API key permissions at https://aistudio.google.com/app/apikey"),
    429: ("GEMINI_RATE_LIMITED",  "Gemini AI is temporarily rate-limited.",
          "You have exceeded the free tier quota. Wait 60 seconds and retry, "
          "or use stored questions via GET /api/v1/quiz/{lang}/books/{book}/{chapter}"),
    500: ("GEMINI_SERVER_ERROR",  "Gemini AI returned an internal error.", "Try again in a moment."),
    503: ("GEMINI_UNAVAILABLE",   "Gemini AI service is temporarily unavailable.", "Try again in a moment."),
}


async def _call_gemini(prompt: str) -> list[dict]:
    """Call Gemini API and return parsed list of question dicts."""
    if not settings.gemini_api_key:
        raise _quiz_error(
            503, "GEMINI_NOT_CONFIGURED",
            "AI question generation is not configured on this server.",
            "The server admin must set GEMINI_API_KEY in environment variables.",
        )

    url = (
        f"{settings.gemini_base_url}/{settings.gemini_model}"
        f":generateContent?key={settings.gemini_api_key}"
    )

    payload = {
        "system_instruction": {"parts": [{"text": _GEMINI_SYSTEM}]},
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.7,
            "maxOutputTokens": 4096,
            "responseMimeType": "application/json",
        },
    }

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(url, json=payload)
    except httpx.TimeoutException:
        raise _quiz_error(
            504, "GEMINI_TIMEOUT",
            "Gemini AI did not respond in time (60 s).",
            "Try with fewer questions (count=3) or a shorter verse range.",
        )
    except httpx.RequestError as exc:
        raise _quiz_error(
            502, "GEMINI_NETWORK_ERROR",
            f"Could not reach Gemini API: {exc}",
            "Check your internet connection or try again later.",
        )

    if resp.status_code != 200:
        code, message, hint = _GEMINI_ERROR_MESSAGES.get(
            resp.status_code,
            ("GEMINI_ERROR", f"Gemini API returned HTTP {resp.status_code}.", None),
        )
        # Try to extract Gemini's own error message for extra context
        try:
            gemini_msg = resp.json().get("error", {}).get("message", "")
            if gemini_msg:
                message = f"{message} Gemini says: {gemini_msg[:200]}"
        except Exception:
            pass
        raise _quiz_error(resp.status_code if resp.status_code in (400, 401, 403, 429) else 502,
                          code, message, hint)

    data = resp.json()

    # Check for safety blocks or empty candidates
    candidates = data.get("candidates", [])
    if not candidates:
        finish = data.get("promptFeedback", {}).get("blockReason", "unknown")
        raise _quiz_error(
            422, "GEMINI_BLOCKED",
            f"Gemini refused to generate questions (block reason: {finish}).",
            "Try rephrasing the request or using a different passage.",
        )

    finish_reason = candidates[0].get("finishReason", "")
    if finish_reason == "SAFETY":
        raise _quiz_error(
            422, "GEMINI_SAFETY_BLOCK",
            "Gemini blocked the response due to safety filters.",
            "Try a different Bible passage.",
        )

    try:
        raw_text = candidates[0]["content"]["parts"][0]["text"]
        # Strip markdown code fences Gemini sometimes wraps around JSON
        raw_text = re.sub(r"^```(?:json)?\s*", "", raw_text.strip())
        raw_text = re.sub(r"\s*```$", "", raw_text.strip())
        questions = json.loads(raw_text)
        if not isinstance(questions, list):
            raise ValueError("Expected a JSON array")
        return questions
    except (KeyError, IndexError, json.JSONDecodeError, ValueError) as exc:
        raise _quiz_error(
            502, "GEMINI_PARSE_ERROR",
            f"Gemini returned a response that could not be parsed as quiz questions: {exc}",
            "This is a transient AI error — try again.",
        )


def _build_prompt(
    book_name: str, chapter: int,
    verse_start: Optional[int], verse_end: Optional[int],
    verses_text: list[dict], count: int,
    difficulty: str, language: str,
) -> str:
    ref = f"{book_name} {chapter}"
    if verse_start:
        ref += f":{verse_start}" + (f"–{verse_end}" if verse_end and verse_end != verse_start else "")

    diff_instruction = (
        f"All questions must be {difficulty} difficulty."
        if difficulty != "mixed"
        else "Mix difficulties: roughly equal beginner, intermediate, and advanced."
    )
    verses_block = "\n".join(f"  v{v['verse']}: {v['text']}" for v in verses_text)

    return (
        f"Bible passage: {ref} ({language.upper()})\n\n"
        f"{verses_block}\n\n"
        f"Generate exactly {count} multiple-choice questions about this passage.\n"
        f"{diff_instruction}\n"
        f"Set verse_start and verse_end to the verse number(s) each question is based on.\n"
        f"If a question spans the whole passage, set both to null.\n"
        f"Return a JSON array of exactly {count} objects."
    )


# ---------------------------------------------------------------------------
# Routes  — IMPORTANT: fixed-path routes (/stats, /random, /generate, /answer)
# MUST appear before the dynamic-param routes (/{lang}/books/...)
# to avoid Starlette matching "stats" or "random" as a {lang} value.
# ---------------------------------------------------------------------------

@router.get("/stats", summary="Quiz question counts by book/lang/difficulty")
async def quiz_stats(db: AsyncSession = Depends(get_db)):
    """How many quiz questions are stored, broken down by book, language, difficulty and source."""
    total = (await db.execute(select(func.count(QuizQuestion.id)))).scalar_one()

    by_book_rows = (await db.execute(
        select(Book.abbreviation, Book.english_name, func.count(QuizQuestion.id))
        .join(QuizQuestion, QuizQuestion.book_id == Book.id)
        .group_by(Book.id).order_by(Book.number)
    )).all()

    by_lang_rows = (await db.execute(
        select(QuizQuestion.language_code, func.count(QuizQuestion.id))
        .group_by(QuizQuestion.language_code)
    )).all()

    by_diff_rows = (await db.execute(
        select(QuizQuestion.difficulty, func.count(QuizQuestion.id))
        .group_by(QuizQuestion.difficulty)
    )).all()

    by_src_rows = (await db.execute(
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
        "by_source": {src: cnt for src, cnt in by_src_rows},
    }


@router.get(
    "/random",
    response_model=list[QuizQuestionOut],
    summary="Random quiz questions",
)
async def get_random_questions(
    lang: str = Query("niv", description="Translation code: niv, en, am, or, ti"),
    book: Optional[str] = Query(None, description="Book abbreviation e.g. GEN (optional)"),
    difficulty: Optional[str] = Query(None, description="beginner / intermediate / advanced"),
    count: int = Query(5, ge=1, le=20, description="Number of questions (1–20)"),
    db: AsyncSession = Depends(get_db),
):
    """Random quiz questions, optionally filtered by language, book, and difficulty."""
    lang = await _resolve_lang(lang, db)

    query = select(QuizQuestion).where(QuizQuestion.language_code == lang)

    if book:
        b = await _resolve_book(book, db)
        query = query.where(QuizQuestion.book_id == b.id)

    if difficulty:
        diff = difficulty.lower()
        if diff not in ("beginner", "intermediate", "advanced"):
            raise _quiz_error(
                400, "INVALID_DIFFICULTY",
                f"Invalid difficulty '{difficulty}'.",
                "Use one of: beginner, intermediate, advanced",
            )
        query = query.where(QuizQuestion.difficulty == diff)

    query = query.order_by(func.random()).limit(count)
    rows = (await db.execute(query)).scalars().all()

    if not rows:
        filters = f"lang={lang}" + (f", book={book}" if book else "") + (f", difficulty={difficulty}" if difficulty else "")
        raise _quiz_error(
            404, "NO_QUESTIONS_FOUND",
            f"No quiz questions found for the given filters ({filters}).",
            "Try GET /api/v1/quiz/stats to see what questions are available, "
            "or POST /api/v1/quiz/generate to create AI questions for any passage.",
        )

    book_ids = list({q.book_id for q in rows})
    books = {b.id: b for b in (await db.execute(select(Book).where(Book.id.in_(book_ids)))).scalars()}
    return [_to_out(q, books[q.book_id]) for q in rows]


@router.post(
    "/generate",
    response_model=GenerateQuizResponse,
    summary="AI-generate quiz questions via Gemini",
)
async def generate_questions(
    req: GenerateQuizRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Use Gemini AI to generate 1–10 quiz questions for any Bible verse or chapter.

    - Fetches the verse text from the database for the given language
    - Sends it to Gemini with a structured prompt
    - Returns fully-formed quiz questions: options A–D, correct answer, explanation
    - Set `save: true` to persist them for future use (they get real DB IDs)

    **Errors are structured** with `error_code` for programmatic handling:
    - `BOOK_NOT_FOUND` — invalid book abbreviation or number
    - `CHAPTER_OUT_OF_RANGE` — chapter exceeds book's chapter count
    - `LANGUAGE_NOT_FOUND` — unsupported language code
    - `NO_VERSE_TEXT` — verse text not seeded for this lang/book/chapter
    - `GEMINI_NOT_CONFIGURED` — server missing API key
    - `GEMINI_RATE_LIMITED` — free tier quota exceeded (retry after 60 s)
    - `GEMINI_TIMEOUT` — AI took too long; try fewer questions
    - `GEMINI_PARSE_ERROR` — transient AI response issue; retry
    """
    b = await _resolve_book(req.book, db)
    lang = await _resolve_lang(req.language, db)

    if req.chapter < 1 or req.chapter > b.chapter_count:
        raise _quiz_error(
            400, "CHAPTER_OUT_OF_RANGE",
            f"{b.english_name} only has {b.chapter_count} chapters (requested chapter {req.chapter}).",
            f"Use a chapter between 1 and {b.chapter_count}.",
        )

    # Fetch verse text from DB
    verse_q = (
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
        verse_q = verse_q.where(Verse.verse >= req.verse_start, Verse.verse <= v_end)

    verse_rows = (await db.execute(verse_q)).scalars().all()

    if not verse_rows:
        ref = f"{b.english_name} {req.chapter}" + (f":{req.verse_start}" if req.verse_start else "")
        raise _quiz_error(
            404, "NO_VERSE_TEXT",
            f"No {lang.upper()} text found for {ref}.",
            f"Verify the language '{lang}' is seeded. "
            f"GET /api/v1/coverage/{lang} shows what's available.",
        )

    verses_text = [{"verse": v.verse, "text": v.text} for v in verse_rows]

    prompt = _build_prompt(
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

    # Build model objects
    new_questions: list[QuizQuestion] = []
    for rq in raw_questions[: req.count]:
        vs = rq.get("verse_start") or req.verse_start
        ve = rq.get("verse_end") or req.verse_end or vs
        new_questions.append(
            QuizQuestion(
                book_id=b.id,
                language_code=lang,
                chapter=req.chapter,
                verse_start=vs,
                verse_end=ve,
                question=str(rq.get("question", "")).strip(),
                option_a=str(rq.get("option_a", "")).strip(),
                option_b=str(rq.get("option_b", "")).strip(),
                option_c=str(rq.get("option_c", "")).strip(),
                option_d=str(rq.get("option_d", "")).strip(),
                correct_answer=str(rq.get("correct_answer", "A")).strip().upper()[0],
                explanation=rq.get("explanation"),
                difficulty=rq.get("difficulty", "beginner"),
                source="ai_generated",
                author="Gemini AI",
                is_verified=False,
            )
        )

    saved = False
    if req.save and new_questions:
        db.add_all(new_questions)
        await db.flush()   # DB assigns real IDs
        await db.commit()
        saved = True
    else:
        # Assign temporary negative IDs so the schema is valid
        for idx, qq in enumerate(new_questions):
            qq.id = -(idx + 1)

    verse_ref = _verse_ref(b.abbreviation, req.chapter, req.verse_start, req.verse_end)

    return GenerateQuizResponse(
        book=b.abbreviation,
        book_name=b.english_name,
        chapter=req.chapter,
        verse_start=req.verse_start,
        verse_end=req.verse_end,
        verse_ref=verse_ref,
        language=lang,
        generated=len(new_questions),
        saved=saved,
        questions=[_to_out(q, b) for q in new_questions],
    )


@router.post(
    "/answer",
    response_model=QuizAnswerResult,
    summary="Submit an answer and get result",
)
async def submit_answer(
    body: QuizAnswerSubmit,
    db: AsyncSession = Depends(get_db),
):
    """
    Submit your answer to a stored question.
    Returns whether it is correct plus the explanation.

    Note: AI-generated questions with negative IDs (not saved) cannot be answered here.
    Set `save: true` in /generate to persist questions with real IDs first.
    """
    if body.question_id < 0:
        raise _quiz_error(
            400, "UNSAVED_QUESTION",
            f"Question ID {body.question_id} is a temporary ID for an unsaved AI-generated question.",
            "Re-generate with save=true to get a persistent ID, then submit your answer.",
        )

    result = await db.execute(select(QuizQuestion).where(QuizQuestion.id == body.question_id))
    qq = result.scalar_one_or_none()
    if not qq:
        raise _quiz_error(
            404, "QUESTION_NOT_FOUND",
            f"No question with id={body.question_id} found.",
            "Use GET /api/v1/quiz/stats to see available questions.",
        )

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


@router.get(
    "/{lang}/books/{book}/{chapter}",
    response_model=QuizListOut,
    summary="Stored questions for a chapter",
)
async def get_chapter_questions(
    lang: str,
    book: str,
    chapter: int,
    difficulty: Optional[str] = Query(None, description="beginner / intermediate / advanced"),
    source: Optional[str] = Query(None, description="static / ai_generated"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    """
    Get stored (curated or AI-saved) quiz questions for a specific chapter.
    Supports filtering by difficulty and source.
    """
    lang = await _resolve_lang(lang, db)
    b = await _resolve_book(book, db)

    if chapter < 1 or chapter > b.chapter_count:
        raise _quiz_error(
            400, "CHAPTER_OUT_OF_RANGE",
            f"{b.english_name} only has {b.chapter_count} chapters.",
            f"Use a chapter between 1 and {b.chapter_count}.",
        )

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
    if source:
        query = query.where(QuizQuestion.source == source.lower())

    total = (await db.execute(select(func.count()).select_from(query.subquery()))).scalar_one()
    rows = (await db.execute(query.offset((page - 1) * page_size).limit(page_size))).scalars().all()

    if total == 0:
        # Helpful hint about AI generation
        raise _quiz_error(
            404, "NO_QUESTIONS_FOUND",
            f"No quiz questions found for {b.english_name} chapter {chapter} ({lang.upper()}).",
            f"Generate questions on-demand via POST /api/v1/quiz/generate "
            f"with book='{b.abbreviation}', chapter={chapter}, language='{lang}'. "
            f"Currently {b.english_name} has questions only for NIV. "
            f"GET /api/v1/quiz/stats shows full coverage.",
        )

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


@router.get(
    "/{lang}/books/{book}/{chapter}/{verse}",
    response_model=QuizListOut,
    summary="Stored questions for a specific verse",
)
async def get_verse_questions(
    lang: str,
    book: str,
    chapter: int,
    verse: int,
    difficulty: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """Get stored questions whose verse range includes a specific verse number."""
    lang = await _resolve_lang(lang, db)
    b = await _resolve_book(book, db)

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

    if not rows:
        raise _quiz_error(
            404, "NO_QUESTIONS_FOUND",
            f"No quiz questions found for {b.english_name} {chapter}:{verse} ({lang.upper()}).",
            f"Generate questions via POST /api/v1/quiz/generate "
            f"with book='{b.abbreviation}', chapter={chapter}, verse_start={verse}, language='{lang}'.",
        )

    return QuizListOut(
        total=len(rows),
        page=1,
        page_size=len(rows),
        book=b.abbreviation,
        book_name=b.english_name,
        chapter=chapter,
        language=lang,
        questions=[_to_out(q, b) for q in rows],
    )
