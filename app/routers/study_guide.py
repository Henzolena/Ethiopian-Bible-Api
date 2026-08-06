"""
LLM-prepared Bible study guides for live group sessions.

This router turns a selected Bible passage into a full session guide so users
can read, reflect, discuss, quiz, pray, and recap without leaving the app.
"""

import json
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app import ai
from app.database import get_db
from app.models import Book, BookName, Verse
from app.routers.books import _resolve_book, _resolve_language

router = APIRouter(prefix="/study-guide", tags=["Study Guide"])


class StudyGuideRequest(BaseModel):
    book: str = Field(description="Book abbreviation, number, or English name")
    chapter: int = Field(ge=1)
    verse_start: int | None = Field(None, ge=1)
    verse_end: int | None = Field(None, ge=1)
    language: str = Field("niv", description="Bible language/translation code")


class StudyGuideTerm(BaseModel):
    term: str
    meaning: str


class StudyGuideRead(BaseModel):
    summary: str
    context: str
    key_observations: list[str]
    key_terms: list[StudyGuideTerm]


class StudyGuideReflect(BaseModel):
    devotional: str
    personal_questions: list[str]
    application: str


class StudyGuideDiscuss(BaseModel):
    opening_question: str
    discussion_questions: list[str]
    leader_notes: list[str]


class StudyGuideQuizQuestion(BaseModel):
    question: str
    options: list[str]
    answer_index: int = Field(ge=0, le=3)
    explanation: str


class StudyGuideQuiz(BaseModel):
    questions: list[StudyGuideQuizQuestion]


class StudyGuidePray(BaseModel):
    prayer_points: list[str]
    guided_prayer: str


class StudyGuideRecap(BaseModel):
    main_takeaway: str
    memory_phrase: str
    next_step: str
    closing_summary: str


class StudyGuide(BaseModel):
    read: StudyGuideRead
    reflect: StudyGuideReflect
    discuss: StudyGuideDiscuss
    quiz: StudyGuideQuiz
    pray: StudyGuidePray
    recap: StudyGuideRecap


class StudyGuideResponse(BaseModel):
    book: str
    book_name: str
    chapter: int
    verse_start: int | None
    verse_end: int | None
    verse_ref: str
    language: str
    generated_at: str
    guide: StudyGuide


def _guide_error(status: int, code: str, message: str, hint: str | None = None) -> HTTPException:
    detail: dict[str, Any] = {"error": True, "error_code": code, "message": message}
    if hint:
        detail["hint"] = hint
    return HTTPException(status_code=status, detail=detail)


async def _book_name(book_id: int, language_id: int, fallback: str, db: AsyncSession) -> str:
    result = await db.execute(
        select(BookName.name).where(
            BookName.book_id == book_id,
            BookName.language_id == language_id,
        )
    )
    return result.scalar_one_or_none() or fallback


def _verse_ref(book: Book, chapter: int, verse_start: int | None, verse_end: int | None) -> str:
    if verse_start is None:
        return f"{book.abbreviation} {chapter}"
    if verse_end and verse_end != verse_start:
        return f"{book.abbreviation} {chapter}:{verse_start}-{verse_end}"
    return f"{book.abbreviation} {chapter}:{verse_start}"


async def _fetch_passage(
    book: Book,
    language_id: int,
    chapter: int,
    verse_start: int | None,
    verse_end: int | None,
    db: AsyncSession,
) -> list[Verse]:
    if chapter < 1 or chapter > book.chapter_count:
        raise _guide_error(
            400,
            "CHAPTER_OUT_OF_RANGE",
            f"{book.english_name} only has {book.chapter_count} chapters.",
        )

    query = select(Verse).where(
        Verse.book_id == book.id,
        Verse.language_id == language_id,
        Verse.chapter == chapter,
    )

    if verse_start is not None:
        end = verse_end or verse_start
        if end < verse_start:
            raise _guide_error(400, "INVALID_RANGE", "verse_end must be greater than or equal to verse_start.")
        query = query.where(Verse.verse >= verse_start, Verse.verse <= end)

    result = await db.execute(query.order_by(Verse.verse))
    verses = list(result.scalars().all())
    if not verses:
        raise _guide_error(404, "PASSAGE_NOT_FOUND", "No Bible text was found for this passage and language.")
    return verses


def _passage_text(verses: list[Verse]) -> str:
    return "\n".join(f"{verse.verse}. {verse.text}" for verse in verses)


def _language_instruction(language_code: str) -> str:
    if language_code == "am":
        return "Write the entire guide in Amharic."
    if language_code == "or":
        return "Write the entire guide in Afaan Oromo."
    if language_code == "ti":
        return "Write the entire guide in Tigrigna."
    return "Write the entire guide in clear English."


_TASK_RULES = """\
You prepare complete Bible study session guides for a mobile app.

Rules:
- Use only the supplied passage as the primary source.
- Be pastoral, practical, and faithful to the passage.
- Do not tell users to go elsewhere for resources.
- Include complete content for every phase: read, reflect, discuss, quiz, pray, recap.
- Quiz must include exactly 3 multiple-choice questions.
- Each quiz question must have exactly 4 options and answer_index must be 0, 1, 2, or 3.
- Return ONLY valid JSON matching this exact shape:
{
  "read": {
    "summary": "...",
    "context": "...",
    "key_observations": ["...", "...", "..."],
    "key_terms": [{"term": "...", "meaning": "..."}]
  },
  "reflect": {
    "devotional": "...",
    "personal_questions": ["...", "...", "..."],
    "application": "..."
  },
  "discuss": {
    "opening_question": "...",
    "discussion_questions": ["...", "...", "..."],
    "leader_notes": ["...", "..."]
  },
  "quiz": {
    "questions": [
      {
        "question": "...",
        "options": ["...", "...", "...", "..."],
        "answer_index": 0,
        "explanation": "..."
      }
    ]
  },
  "pray": {
    "prayer_points": ["...", "...", "..."],
    "guided_prayer": "..."
  },
  "recap": {
    "main_takeaway": "...",
    "memory_phrase": "...",
    "next_step": "...",
    "closing_summary": "..."
  }
}
"""

# The shared contract overrides these task rules — see app/ai/contract.py.
_SYSTEM_PROMPT = ai.system_prompt(_TASK_RULES)


def _build_prompt(
    *,
    language_code: str,
    book_name: str,
    verse_ref: str,
    passage: str,
) -> str:
    return f"""\
Prepare a complete Bible study guide for this passage.

Language instruction: {_language_instruction(language_code)}
Reference: {verse_ref}
Book: {book_name}

Bible text:
{passage}

Make the guide usable inside the app as-is. Keep each section concise enough for a live group session, but include real content, not instructions to create content later.
"""


async def _call_mistral(prompt: str) -> dict[str, Any]:
    if not settings.mistral_api_key:
        raise _guide_error(
            503,
            "AI_NOT_CONFIGURED",
            "Study guide generation is not configured on this server.",
            "Set MISTRAL_API_KEY in Railway Variables.",
        )

    last_parse_error: Exception | None = None

    try:
        async with httpx.AsyncClient(timeout=90) as client:
            for attempt in range(2):
                retry_prompt = prompt
                if attempt == 1:
                    retry_prompt = (
                        f"{prompt}\n\n"
                        "Retry instruction: the previous JSON was invalid. Keep every field, but make each "
                        "string shorter so the response fits completely. Return only valid JSON."
                    )

                response = await client.post(
                    f"{settings.mistral_base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {settings.mistral_api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": settings.mistral_model,
                        "messages": [
                            {"role": "system", "content": _SYSTEM_PROMPT},
                            {"role": "user", "content": retry_prompt},
                        ],
                        "response_format": {"type": "json_object"},
                        "temperature": 0.35 if attempt == 0 else 0.2,
                        "max_tokens": 8192,
                    },
                )

                if response.status_code != 200:
                    message = f"Mistral API returned HTTP {response.status_code}."
                    try:
                        payload = response.json()
                        api_message = payload.get("message") or payload.get("error", {}).get("message")
                        if api_message:
                            message = f"{message} {str(api_message)[:200]}"
                    except Exception:
                        pass

                    code = "AI_ERROR"
                    if response.status_code == 401:
                        code = "AI_UNAUTHORIZED"
                    elif response.status_code == 429:
                        code = "AI_RATE_LIMITED"
                    elif response.status_code in (500, 503):
                        code = "AI_UNAVAILABLE"

                    raise _guide_error(
                        response.status_code if response.status_code in (401, 422, 429) else 502,
                        code,
                        message,
                    )

                try:
                    content = response.json()["choices"][0]["message"]["content"]
                    data = json.loads(content)
                    if isinstance(data, dict) and isinstance(data.get("guide"), dict):
                        data = data["guide"]
                    if not isinstance(data, dict):
                        raise ValueError("Top-level JSON was not an object.")
                    return data
                except (KeyError, IndexError, json.JSONDecodeError, ValueError) as exc:
                    last_parse_error = exc
                    continue
    except httpx.TimeoutException:
        raise _guide_error(504, "AI_TIMEOUT", "Study guide generation timed out.", "Try again in a moment.")
    except httpx.RequestError as exc:
        raise _guide_error(502, "AI_NETWORK_ERROR", f"Could not reach Mistral API: {exc}")

    raise _guide_error(
        502,
        "AI_PARSE_ERROR",
        f"Mistral returned a study guide that could not be parsed: {last_parse_error}",
        "Try again. The next response usually fixes the JSON shape.",
    )


@router.post("/generate", response_model=StudyGuideResponse)
async def generate_study_guide(
    request: StudyGuideRequest,
    db: AsyncSession = Depends(get_db),
):
    language = await _resolve_language(request.language, db)
    book = await _resolve_book(request.book, db)
    verses = await _fetch_passage(
        book=book,
        language_id=language.id,
        chapter=request.chapter,
        verse_start=request.verse_start,
        verse_end=request.verse_end,
        db=db,
    )
    book_name = await _book_name(book.id, language.id, book.english_name, db)
    verse_ref = _verse_ref(book, request.chapter, request.verse_start, request.verse_end)
    prompt = _build_prompt(
        language_code=language.code,
        book_name=book_name,
        verse_ref=verse_ref,
        passage=_passage_text(verses),
    )

    passage = _passage_text(verses)

    # A guide is one composite item rather than a list, so it goes through the
    # pipeline with want=1. Previously the only check was Pydantic shape
    # validation, which cannot tell whether the devotional is grounded in the
    # passage or whether the quiz inside it is answerable.
    async def _generate(_n: int) -> list[dict[str, Any]]:
        return [await _call_mistral(prompt)]

    def _validate(candidate: dict[str, Any]) -> ai.Verdict:
        verdicts: list[ai.Verdict] = []

        # Shape first: everything below assumes the sections exist.
        try:
            StudyGuide.model_validate(candidate)
        except Exception as exc:  # pydantic ValidationError
            v = ai.Verdict(ok=True)
            return v.fail(f"guide shape invalid: {type(exc).__name__}")

        # The embedded quiz must be answerable — same bar as /quiz/generate.
        for i, q in enumerate(candidate.get("quiz", {}).get("questions") or []):
            options = [str(o or "").strip() for o in (q.get("options") or [])]
            idx = q.get("answer_index")
            letter = (
                ("A", "B", "C", "D")[idx]
                if isinstance(idx, int) and 0 <= idx < min(4, len(options))
                else None
            )
            qv = ai.check_mcq(
                str(q.get("question") or ""), options, letter, q.get("explanation")
            )
            if not qv.ok:
                qv.reasons = [f"quiz q{i + 1}: {r}" for r in qv.reasons]
            verdicts.append(qv)

        # Grounding on the parts that assert what the passage says.
        read = candidate.get("read") or {}
        verdicts.append(
            ai.check_grounding(
                " ".join(
                    str(x) for x in (read.get("summary"), read.get("context")) if x
                ),
                passage,
                threshold=0.20,
            )
        )

        # Safety across every free-text field a reader will actually see.
        reflect = candidate.get("reflect") or {}
        pray = candidate.get("pray") or {}
        verdicts.append(
            ai.screen_safety(
                read.get("summary"),
                read.get("context"),
                reflect.get("devotional"),
                reflect.get("application"),
                pray.get("guided_prayer"),
                *(reflect.get("personal_questions") or []),
                *(pray.get("prayer_points") or []),
            )
        )

        return ai.merge(*verdicts)

    accepted, stats = await ai.run(
        kind="study_guide",
        passage_ref=verse_ref,
        passage_text=passage,
        want=1,
        generate=_generate,
        validate=_validate,
    )

    if not accepted:
        raise _guide_error(
            502, "AI_QUALITY_GATE",
            "No study guide met the quality bar for this passage.",
            "Try a wider verse range. Rejections: "
            f"{'; '.join(stats.reasons[:3]) or 'none recorded'}",
        )

    guide = StudyGuide.model_validate(accepted[0])

    return StudyGuideResponse(
        book=book.abbreviation,
        book_name=book_name,
        chapter=request.chapter,
        verse_start=request.verse_start,
        verse_end=request.verse_end,
        verse_ref=verse_ref,
        language=language.code,
        generated_at=datetime.now(timezone.utc).isoformat(),
        guide=guide,
    )
