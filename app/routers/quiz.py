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

import asyncio
import json
import re
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import Book, Language, QuizQuestion, Verse
from app.schemas import (
    GenerateAllLanguagesRequest,
    GenerateAllLanguagesResponse,
    GenerateQuizRequest,
    GenerateQuizResponse,
    QuizAnswerResult,
    QuizAnswerSubmit,
    QuizListOut,
    QuizOption,
    QuizQuestionOut,
)

router = APIRouter(prefix="/quiz", tags=["Quiz & Trivia"])


class PracticeTranslateRequest(BaseModel):
    target_language: str = Field(description="am / or / ti")
    book: str = Field(description="Book abbreviation e.g. MRK")
    book_name: str = Field(description="English book name e.g. Mark")
    chapter: int
    verse: int
    verse_ref: str
    kind: str = Field(description="mcq or fill")
    prompt: str
    options: list[str]
    answer_index: int
    fill_pre: str | None = None
    fill_post: str | None = None


class PracticeTranslateResponse(BaseModel):
    language: str
    verse_ref: str
    kind: str
    prompt: str
    options: list[str]
    answer_index: int
    verse_text: str | None = None


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
        group_id=q.group_id,
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
# Mistral AI — question generation (primary provider)
# Free tier: 1 B tokens/month | Model: mistral-small-latest (Small 3.1)
# OpenAI-compatible chat completions API with JSON-object response mode.
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are a Bible quiz question writer. Given Bible verse text you will generate
multiple-choice trivia questions suitable for a Bible study app.

RULES:
- Each question must have exactly 4 options: A, B, C, D
- Exactly one option must be correct
- Wrong options (distractors) must be plausible but clearly wrong based on the text
- Do NOT include the answer letter in the question text
- Include a brief explanation (1-2 sentences) of why the answer is correct
- Vary difficulty if asked for mixed
- difficulty values: "beginner", "intermediate", "advanced"
- Return ONLY valid JSON matching the OUTPUT FORMAT below — no extra text

OUTPUT FORMAT (JSON object with a "questions" array):
{
  "questions": [
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
}
"""

# Mistral HTTP status → structured error
_MISTRAL_ERRORS = {
    401: ("AI_UNAUTHORIZED",  "Mistral API key is invalid.",
          "Set a valid MISTRAL_API_KEY in the Railway Variables dashboard."),
    422: ("AI_BAD_REQUEST",   "Mistral rejected the request.", None),
    429: ("AI_RATE_LIMITED",  "AI generation is temporarily rate-limited.",
          "Mistral's free tier is very generous (1B tokens/month) — "
          "this is a short burst limit. Wait a few seconds and retry."),
    500: ("AI_SERVER_ERROR",  "Mistral returned an internal error.", "Try again in a moment."),
    503: ("AI_UNAVAILABLE",   "Mistral service is temporarily unavailable.", "Try again in a moment."),
}


async def _call_mistral(prompt: str) -> list[dict]:
    """Call Mistral chat-completions API and return a list of raw question dicts."""
    if not settings.mistral_api_key:
        raise _quiz_error(
            503, "AI_NOT_CONFIGURED",
            "AI question generation is not configured on this server.",
            "The server admin must set MISTRAL_API_KEY in the Railway Variables dashboard.",
        )

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{settings.mistral_base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {settings.mistral_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": settings.mistral_model,
                    "messages": [
                        {"role": "system", "content": _SYSTEM_PROMPT},
                        {"role": "user",   "content": prompt},
                    ],
                    "response_format": {"type": "json_object"},
                    "temperature": 0.7,
                    "max_tokens": 4096,
                },
            )
    except httpx.TimeoutException:
        raise _quiz_error(
            504, "AI_TIMEOUT",
            "Mistral AI did not respond in time (60 s).",
            "Try with fewer questions (count=3) or a shorter verse range.",
        )
    except httpx.RequestError as exc:
        raise _quiz_error(
            502, "AI_NETWORK_ERROR",
            f"Could not reach Mistral API: {exc}",
            "Check server connectivity or try again later.",
        )

    if resp.status_code != 200:
        code, message, hint = _MISTRAL_ERRORS.get(
            resp.status_code,
            ("AI_ERROR", f"Mistral API returned HTTP {resp.status_code}.", None),
        )
        try:
            api_msg = resp.json().get("message", "")
            if api_msg:
                message = f"{message} ({api_msg[:200]})"
        except Exception:
            pass
        raise _quiz_error(
            resp.status_code if resp.status_code in (401, 422, 429) else 502,
            code, message, hint,
        )

    try:
        content = resp.json()["choices"][0]["message"]["content"]
        data = json.loads(content)
        # Expect {"questions": [...]} — gracefully handle bare [...] too
        if isinstance(data, dict):
            questions = data.get("questions", [])
        elif isinstance(data, list):
            questions = data
        else:
            raise ValueError(f"Unexpected JSON shape: {type(data)}")
        if not isinstance(questions, list) or len(questions) == 0:
            raise ValueError("'questions' list is empty or missing")
        return questions
    except (KeyError, IndexError, json.JSONDecodeError, ValueError) as exc:
        raise _quiz_error(
            502, "AI_PARSE_ERROR",
            f"Mistral returned a response that could not be parsed as quiz questions: {exc}",
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
    summary="AI-generate quiz questions via Mistral",
)
async def generate_questions(
    req: GenerateQuizRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Use Mistral AI (Small 3.1, free tier) to generate 1–10 quiz questions for any
    Bible verse or chapter.

    - Fetches the verse text from the database for the given language
    - Sends it to Mistral with a structured prompt requesting JSON output
    - Returns fully-formed quiz questions: options A–D, correct answer, explanation
    - Set `save: true` to persist them for future use (they get real DB IDs)

    **Errors are structured** with `error_code` for programmatic handling:
    - `BOOK_NOT_FOUND`       — invalid book abbreviation or number
    - `CHAPTER_OUT_OF_RANGE` — chapter exceeds book's chapter count
    - `LANGUAGE_NOT_FOUND`   — unsupported language code
    - `NO_VERSE_TEXT`        — verse text not seeded for this lang/book/chapter
    - `AI_NOT_CONFIGURED`    — server missing MISTRAL_API_KEY
    - `AI_RATE_LIMITED`      — burst limit hit; wait a few seconds and retry
    - `AI_TIMEOUT`           — AI took too long; try fewer questions (count=3)
    - `AI_PARSE_ERROR`       — transient AI response issue; retry
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

    raw_questions = await _call_mistral(prompt)

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
                author="Mistral AI",
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


# ---------------------------------------------------------------------------
# Multi-language generation helpers
# ---------------------------------------------------------------------------

_LANG_NAMES = {
    "am":  "Amharic (አማርኛ)",
    "or":  "Oromo (Afaan Oromoo)",
    "ti":  "Tigrigna (ትግርኛ)",
    "en":  "English (KJV)",
    "niv": "English (NIV)",
}

_TRANSLATION_LANGUAGES = ["am", "or", "ti"]   # non-English targets


async def _fetch_chapter_verses(
    lang: str, book_id: int, chapter: int, db: AsyncSession
) -> list[dict]:
    """Returns [{verse: int, text: str}, ...] for a given language + book + chapter."""
    code = lang.lower()
    rows = (await db.execute(
        select(Verse)
        .join(Language, Verse.language_id == Language.id)
        .where(Verse.book_id == book_id, Language.code == code, Verse.chapter == chapter)
        .order_by(Verse.verse)
    )).scalars().all()
    return [{"verse": v.verse, "text": v.text} for v in rows]


async def _translate_with_gemini(
    english_questions: list[dict],
    target_lang: str,
    book_name: str,
    chapter: int,
    native_verses: list[dict],
) -> list[dict] | None:
    """
    Use Gemini to translate English quiz questions into the target language.
    Native verse text is provided so Gemini can use canonical terminology.
    Returns translated question dicts or None on failure.
    """
    keys = [k for k in [
        settings.gemini_api_key,
        settings.gemini_api_key_henokrobale,
        settings.gemini_api_key_eeccaustinchurch,
        settings.gemini_api_key_eeccaustinapp,
        settings.gemini_api_key_henzolinasj,
        settings.gemini_api_key_harmonikahn,
        settings.gemini_api_key_robalehenok,
    ] if k]
    if not keys:
        return None

    lang_name   = _LANG_NAMES.get(target_lang, target_lang.upper())
    verses_block = "\n".join(f"  {v['verse']}: {v['text']}" for v in native_verses)
    eq_json     = json.dumps(english_questions, ensure_ascii=False, indent=2)

    prompt = (
        f"Translate the following Bible quiz questions from English into {lang_name}.\n\n"
        f"Native {lang_name} Bible text for {book_name} chapter {chapter} "
        f"(use this as your reference for names, places, and terminology):\n"
        f"{verses_block}\n\n"
        f"English questions (JSON):\n{eq_json}\n\n"
        f"TRANSLATION RULES:\n"
        f"- Translate ALL text fields: question, option_a, option_b, option_c, option_d, explanation\n"
        f"- Do NOT change: correct_answer (A/B/C/D), verse_start, verse_end, difficulty\n"
        f"- Use natural, fluent {lang_name} — not word-for-word literal translation\n"
        f"- Use terminology from the native Bible text above for consistency\n"
        f"- Return ONLY a JSON object: {{\"questions\": [ ... same structure ... ]}}\n"
    )

    url_template = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{settings.gemini_model}:generateContent?key={{key}}"
    )
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": 0.3,
            "maxOutputTokens": 8192,
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }

    try:
        async with httpx.AsyncClient(timeout=90) as http:
            for i, key in enumerate(keys):
                label = f"key {i+1}/{len(keys)}"
                resp = await http.post(url_template.format(key=key), json=payload)

                if resp.status_code == 429 and i + 1 < len(keys):
                    print(f"[QuizML] Gemini 429 for {target_lang} on {label} — trying next key")
                    await asyncio.sleep(2)
                    continue

                if resp.status_code != 200:
                    print(f"[QuizML] Gemini {resp.status_code} for {target_lang} on {label}: {resp.text[:200]}")
                    return None

                content = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
                data    = json.loads(content)
                qs      = data.get("questions", data) if isinstance(data, dict) else data
                return qs if isinstance(qs, list) else None

        return None

    except Exception as exc:
        print(f"[QuizML] Translation failed for {target_lang}: {exc}")
        return None


async def _save_questions(
    raw_questions: list[dict],
    book: Book,
    chapter: int,
    lang: str,
    author: str,
    db: AsyncSession,
    group_ids: list[str] | None = None,  # when provided, links each question to its group
) -> int:
    """Inserts questions into the DB. Returns count saved."""
    import uuid as _uuid
    rows = []
    for i, rq in enumerate(raw_questions):
        vs = rq.get("verse_start")
        ve = rq.get("verse_end") or vs
        q  = rq.get("question", "").strip()
        oa = rq.get("option_a", "").strip()
        ob = rq.get("option_b", "").strip()
        oc = rq.get("option_c", "").strip()
        od = rq.get("option_d", "").strip()
        if not (q and oa and ob and oc and od):
            continue
        gid = (group_ids[i] if group_ids and i < len(group_ids)
               else str(_uuid.uuid4()))
        rows.append(QuizQuestion(
            group_id=gid,
            book_id=book.id,
            language_code=lang,
            chapter=chapter,
            verse_start=vs, verse_end=ve,
            question=q, option_a=oa, option_b=ob, option_c=oc, option_d=od,
            correct_answer=str(rq.get("correct_answer", "A")).strip().upper()[0],
            explanation=rq.get("explanation"),
            difficulty=rq.get("difficulty", "beginner"),
            source="ai_generated",
            author=author,
            is_verified=False,
        ))
    if rows:
        db.add_all(rows)
        await db.flush()
        await db.commit()
    return len(rows)


# ---------------------------------------------------------------------------
# Multi-language generation endpoint
# ---------------------------------------------------------------------------

@router.get(
    "/by-groups",
    response_model=list[QuizQuestionOut],
    summary="Fetch translated versions of specific questions by group_id",
)
async def get_questions_by_groups(
    group_ids: str = Query(description="Comma-separated list of group_id UUIDs"),
    lang:      str = Query("am", description="Target language code: am, or, ti, niv"),
    db: AsyncSession = Depends(get_db),
):
    """
    Given a list of group_ids (from questions already loaded in one language),
    return the same questions in a different language.
    This is what the iOS language switcher calls to swap quiz content without
    showing completely different questions.
    """
    ids = [g.strip() for g in group_ids.split(",") if g.strip()]
    if not ids:
        return []

    lang = await _resolve_lang(lang, db)

    rows = (await db.execute(
        select(QuizQuestion)
        .where(QuizQuestion.group_id.in_(ids), QuizQuestion.language_code == lang)
        .order_by(QuizQuestion.id)
    )).scalars().all()

    if not rows:
        return []

    book_ids = list({q.book_id for q in rows})
    books    = {b.id: b for b in (await db.execute(select(Book).where(Book.id.in_(book_ids)))).scalars()}

    # Return in the same order as the requested group_ids
    id_to_q  = {q.group_id: q for q in rows}
    ordered  = [id_to_q[gid] for gid in ids if gid in id_to_q]
    return [_to_out(q, books[q.book_id]) for q in ordered]


@router.post(
    "/translate-practice",
    response_model=PracticeTranslateResponse,
    summary="Translate one local Verse Pack practice question",
)
async def translate_practice_question(
    req: PracticeTranslateRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Translate the exact local Verse Pack practice question currently displayed
    in the app. This keeps the same answer index/options instead of swapping in
    a different generated chapter question.
    """
    target = req.target_language.lower()
    if target not in _TRANSLATION_LANGUAGES:
        raise _quiz_error(400, "LANGUAGE_NOT_SUPPORTED", "Practice translation supports am, or, and ti.")

    b = await _resolve_book(req.book, db)
    if req.chapter < 1 or req.chapter > b.chapter_count:
        raise _quiz_error(400, "CHAPTER_OUT_OF_RANGE", f"{b.english_name} only has {b.chapter_count} chapters.")

    native_verses = await _fetch_chapter_verses(target, b.id, req.chapter, db)
    native_verse_text = next((v["text"] for v in native_verses if v["verse"] == req.verse), None)

    raw_question = {
        "question": req.prompt,
        "option_a": req.options[0] if len(req.options) > 0 else "",
        "option_b": req.options[1] if len(req.options) > 1 else "",
        "option_c": req.options[2] if len(req.options) > 2 else "",
        "option_d": req.options[3] if len(req.options) > 3 else "",
        "correct_answer": ["A", "B", "C", "D"][max(0, min(req.answer_index, 3))],
        "verse_start": req.verse,
        "verse_end": req.verse,
        "difficulty": "regular",
        "explanation": "",
    }

    translated = await _translate_with_gemini(
        english_questions=[raw_question],
        target_lang=target,
        book_name=req.book_name,
        chapter=req.chapter,
        native_verses=native_verses,
    )
    if not translated:
        raise _quiz_error(
            502,
            "TRANSLATION_FAILED",
            "The practice question could not be translated right now.",
            "Try again in a moment.",
        )

    first = translated[0]
    translated_options = [
        first.get("option_a", ""),
        first.get("option_b", ""),
        first.get("option_c", ""),
        first.get("option_d", ""),
    ]
    answer_letter = str(first.get("correct_answer", raw_question["correct_answer"])).strip().upper()[:1]
    answer_index = {"A": 0, "B": 1, "C": 2, "D": 3}.get(answer_letter, req.answer_index)

    return PracticeTranslateResponse(
        language=target,
        verse_ref=req.verse_ref,
        kind=req.kind,
        prompt=first.get("question", req.prompt),
        options=translated_options,
        answer_index=answer_index,
        verse_text=native_verse_text,
    )


@router.post(
    "/generate-all-languages",
    response_model=GenerateAllLanguagesResponse,
    summary="Generate quiz questions in EN + AM + OR + TI simultaneously",
)
async def generate_all_languages(
    req: GenerateAllLanguagesRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Generates quiz questions for all 4 Bible languages in one call:

    1. Validates book + chapter.
    2. Generates English (NIV) questions using Mistral (primary quality source).
    3. In parallel: fetches native verse text for AM, OR, TI and uses Gemini
       to translate the English questions into each language.
    4. Saves all language versions to the database.

    After this call, users can switch between AM/EN/OR/TI in the quiz UI
    and always get questions for the chapter.
    """
    b    = await _resolve_book(req.book, db)
    lang = "niv"   # always generate from NIV as the English source

    if req.chapter < 1 or req.chapter > b.chapter_count:
        raise _quiz_error(
            400, "CHAPTER_OUT_OF_RANGE",
            f"{b.english_name} only has {b.chapter_count} chapters.",
            f"Use a chapter between 1 and {b.chapter_count}.",
        )

    import uuid as _uuid
    from sqlalchemy import delete as sa_delete

    # ── Step 1: DELETE all existing questions for this chapter (clean slate) ─
    # This ensures the new coordinated set is the only one users see.
    if req.save:
        await db.execute(
            sa_delete(QuizQuestion).where(
                QuizQuestion.book_id == b.id,
                QuizQuestion.chapter == req.chapter,
            )
        )
        await db.commit()

    # ── Step 2: fetch English verse text and generate primary questions ───────
    en_verses = await _fetch_chapter_verses(lang, b.id, req.chapter, db)
    if not en_verses:
        raise _quiz_error(
            404, "NO_VERSE_TEXT",
            f"No NIV text found for {b.english_name} {req.chapter}.",
            "Ensure the NIV translation is seeded in the database.",
        )

    prompt = _build_prompt(
        book_name=b.english_name, chapter=req.chapter,
        verse_start=None, verse_end=None,
        verses_text=en_verses, count=req.count,
        difficulty=req.difficulty or "mixed", language="NIV",
    )
    en_raw = await _call_mistral(prompt)

    # Assign a stable group_id to each English question.
    # All translations of question N will share the same group_id.
    group_ids = [str(_uuid.uuid4()) for _ in en_raw]

    generated: dict = {}
    errors:    dict = {}

    if req.save:
        saved_en = await _save_questions(
            en_raw, b, req.chapter, lang, "Mistral AI", db, group_ids=group_ids)
        generated[lang] = saved_en
    else:
        generated[lang] = len(en_raw)

    # ── Step 3: fetch native verses SEQUENTIALLY (SQLAlchemy async session is
    # not safe to share across concurrent coroutines in asyncio.gather) ────────
    am_verses = await _fetch_chapter_verses("am", b.id, req.chapter, db)
    or_verses = await _fetch_chapter_verses("or", b.id, req.chapter, db)
    ti_verses = await _fetch_chapter_verses("ti", b.id, req.chapter, db)
    native_map = {"am": am_verses, "or": or_verses, "ti": ti_verses}

    # Gemini translations run in parallel — they make no DB calls so it's safe
    translation_tasks = {
        tl: _translate_with_gemini(en_raw, tl, b.english_name, req.chapter, nvs)
        for tl, nvs in native_map.items()
        if nvs
    }
    results    = await asyncio.gather(*translation_tasks.values(), return_exceptions=True)
    translated = dict(zip(translation_tasks.keys(), results))

    # ── Step 4: save translations with MATCHING group_ids ─────────────────────
    for tl, qs in translated.items():
        if isinstance(qs, Exception) or not qs:
            errors[tl] = str(qs) if isinstance(qs, Exception) else "empty_response"
            generated[tl] = 0
            continue
        if req.save:
            # Pad/trim group_ids to match translated question count
            gids = group_ids[:len(qs)] + [str(_uuid.uuid4()) for _ in range(max(0, len(qs)-len(group_ids)))]
            cnt = await _save_questions(
                qs, b, req.chapter, tl, "Gemini AI", db, group_ids=gids)
            generated[tl] = cnt
        else:
            generated[tl] = len(qs)

    return GenerateAllLanguagesResponse(
        book=b.abbreviation,
        book_name=b.english_name,
        chapter=req.chapter,
        generated_per_language=generated,
        saved=req.save,
        errors=errors,
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
