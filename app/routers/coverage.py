"""
Coverage router.

GET /api/v1/coverage
    → Complete text + audio status for every language × every book.
    Useful for frontend apps to know exactly what's available before making calls.

GET /api/v1/coverage/{lang}
    → Same detail for a single language.
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, text
from app.database import get_db
from app.models import Language, Verse

router = APIRouter(tags=["Coverage"])

# ---------------------------------------------------------------------------
# Static book list  (number, abbr, english_name, testament, chapter_count)
# ---------------------------------------------------------------------------
from scripts.bible_books import BOOKS   # [(num, abbr, eng, testament, chapters, ...), ...]

OT_ABBRS = {row[1] for row in BOOKS if row[3] == "OT"}
NT_ABBRS = {row[1] for row in BOOKS if row[3] == "NT"}
ALL_ABBRS = OT_ABBRS | NT_ABBRS

# ---------------------------------------------------------------------------
# Audio coverage sets — derived from audio.py mappings (kept in sync manually)
# ---------------------------------------------------------------------------

# Amharic: j-e-c.org — full OT except MAL, full NT except LUK
AM_AUDIO = (OT_ABBRS - {"MAL"}) | (NT_ABBRS - {"LUK"})

# Oromo + Tigrigna: archive.org — NT only
ARCHIVE_NT_ABBRS = {
    "MAT","MRK","LUK","JHN","ACT","ROM",
    "1CO","2CO","GAL","EPH","PHP","COL",
    "1TH","2TH","1TI","2TI","TIT","PHM",
    "HEB","JAS","1PE","2PE","1JN","2JN","3JN","JUD","REV",
}

# English KJV + NIV: all 66 books
FULL_66_AUDIO = ALL_ABBRS

# Map lang code → audio abbr set
AUDIO_SETS = {
    "am":  AM_AUDIO,
    "or":  ARCHIVE_NT_ABBRS,
    "ti":  ARCHIVE_NT_ABBRS,
    "en":  FULL_66_AUDIO,
    "niv": FULL_66_AUDIO,
}

# ---------------------------------------------------------------------------
# Static per-language metadata
# ---------------------------------------------------------------------------

LANG_STATIC = {
    "am": {
        "name": "Amharic",
        "native_name": "አማርኛ",
        "text_version": "Ethiopian Protestant Bible (1954/1962)",
        "text_source": "Open-source corpus (magna25 / Amharic Bible project)",
        "audio_version": "Amharic Bible reading — Paulos Haileselassie (2005)",
        "audio_source": "Jerusalem Evangelical Church — j-e-c.org",
        "text_audio_match": True,
        "versification": "ethiopian",
        "versification_note": (
            "The Amharic Bible combines some KJV verse pairs into one verse. "
            "Verse numbers may differ from KJV/NIV by 1–2 in certain chapters "
            "(e.g. John 3 has 34 verses in Amharic vs 36 in KJV)."
        ),
        "audio_missing_ot": ["MAL"],
        "audio_missing_nt": ["LUK"],
        "audio_missing_note": "Malachi (OT) and Luke (NT) not recorded on j-e-c.org",
    },
    "or": {
        "name": "Oromo",
        "native_name": "Afaan Oromoo",
        "text_version": "MACQUL — Macaafa Qulqulluu (Bible Society of Ethiopia)",
        "text_source": "Scraped from Bible.com (YouVersion) — full 66 books",
        "audio_version": "FCBH Oromo NT (Faith Comes By Hearing)",
        "audio_source": "Internet Archive — bible_Audio_Oromo",
        "text_audio_match": True,
        "versification": "standard",
        "versification_note": "Verse numbers match standard (KJV) versification.",
        "audio_missing_ot": list(OT_ABBRS),   # entire OT missing
        "audio_missing_nt": [],
        "audio_missing_note": "Old Testament audio not available — NT only (27 books, 260 chapters)",
    },
    "ti": {
        "name": "Tigrigna",
        "native_name": "ትግርኛ",
        "text_version": "Tigrigna Bible (geezexperience.com)",
        "text_source": "Scraped from geezexperience.com — full 66 books",
        "audio_version": "FCBH Tigrigna NT (Faith Comes By Hearing)",
        "audio_source": "Internet Archive — bible_Audio_Amharictigrinya",
        "text_audio_match": None,   # unverified — translation may differ
        "versification": "ethiopian",
        "versification_note": (
            "Tigrigna text uses Ethiopian versification — some chapters combine verses "
            "(e.g. Romans 8 has 32 verses vs 39 in KJV). "
            "Audio translation source may differ from geezexperience.com text."
        ),
        "audio_missing_ot": list(OT_ABBRS),
        "audio_missing_nt": [],
        "audio_missing_note": "Old Testament audio not available — NT only (27 books, 260 chapters)",
    },
    "en": {
        "name": "English",
        "native_name": "English (KJV)",
        "text_version": "King James Version (KJV) — public domain",
        "text_source": "thiagobodruk/bible on GitHub (en_kjv.json)",
        "audio_version": "King James Version — voice-only narration",
        "audio_source": "AudioTreasure.com — audiotreasure.com/content/KJV_AT/",
        "text_audio_match": True,
        "versification": "standard",
        "versification_note": "Verse numbers and wording match exactly between text and audio.",
        "audio_missing_ot": [],
        "audio_missing_nt": [],
        "audio_missing_note": None,
    },
    "niv": {
        "name": "English",
        "native_name": "English (NIV)",
        "text_version": "New International Version (NIV) — Biblica, 2011",
        "text_source": "bolls.life public API (/get-chapter/NIV/) — full 66 books",
        "audio_version": "NIV Audio Bible — full narration",
        "audio_source": "Internet Archive — englishNIVAudioBible collection",
        "text_audio_match": True,
        "versification": "standard",
        "versification_note": (
            "NIV uses standard versification — verse numbers match KJV/standard. "
            "Text and audio are both the NIV 2011 edition."
        ),
        "audio_missing_ot": [],
        "audio_missing_nt": [],
        "audio_missing_note": None,
    },
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_book_list(audio_set: set[str]) -> list[dict]:
    """Return per-book coverage rows for a language."""
    out = []
    for row in BOOKS:
        num, abbr, eng, testament, chapters = row[0], row[1], row[2], row[3], row[4]
        out.append({
            "number": num,
            "abbreviation": abbr,
            "name": eng,
            "testament": testament,
            "chapters": chapters,
            "text": True,        # all languages have text for all 66 books
            "audio": abbr in audio_set,
        })
    return out


async def _verse_counts(db: AsyncSession) -> dict[str, int]:
    """Return {lang_code: total_verse_count} from the DB."""
    rows = await db.execute(
        text(
            "SELECT l.code, COUNT(v.id) "
            "FROM verses v JOIN languages l ON v.language_id = l.id "
            "GROUP BY l.code"
        )
    )
    return {code: count for code, count in rows.fetchall()}


def _build_lang_coverage(code: str, meta: dict, verse_count: int) -> dict:
    audio_set = AUDIO_SETS.get(code, set())
    books = _build_book_list(audio_set)

    missing_ot = sorted(meta["audio_missing_ot"])
    missing_nt = sorted(meta["audio_missing_nt"])
    total_with_audio = sum(1 for b in books if b["audio"])
    total_missing_audio = 66 - total_with_audio

    return {
        "code": code,
        "name": meta["name"],
        "native_name": meta["native_name"],
        "text": {
            "available": True,
            "total_books": 66,
            "total_verses": verse_count,
            "version": meta["text_version"],
            "source": meta["text_source"],
            "versification": meta["versification"],
            "versification_note": meta["versification_note"],
        },
        "audio": {
            "available": total_with_audio > 0,
            "version": meta["audio_version"],
            "source": meta["audio_source"],
            "text_audio_match": meta["text_audio_match"],
            "text_audio_match_note": (
                "Verified — text and audio are the same translation edition"
                if meta["text_audio_match"] is True
                else "Unverified — audio translation source may differ from text"
                if meta["text_audio_match"] is None
                else "Does not match"
            ),
            "ot": {
                "total_books": 39,
                "books_with_audio": 39 - len([a for a in missing_ot if a in OT_ABBRS]),
                "missing": missing_ot,
            },
            "nt": {
                "total_books": 27,
                "books_with_audio": 27 - len([a for a in missing_nt if a in NT_ABBRS]),
                "missing": missing_nt,
            },
            "total_books_with_audio": total_with_audio,
            "total_books_missing_audio": total_missing_audio,
            "missing_note": meta["audio_missing_note"],
        },
        "books": books,
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/coverage")
async def get_coverage(db: AsyncSession = Depends(get_db)):
    """
    Complete text + audio availability for every language × every book.
    Use this to drive UI state — know exactly which books/chapters support
    audio playback before the user taps play.
    """
    verse_counts = await _verse_counts(db)

    # Ordered: am, or, ti, en, niv
    lang_order = ["am", "or", "ti", "en", "niv"]
    languages = []
    for code in lang_order:
        meta = LANG_STATIC.get(code)
        if not meta:
            continue
        vc = verse_counts.get(code, 0)
        languages.append(_build_lang_coverage(code, meta, vc))

    total_verses = sum(verse_counts.get(c, 0) for c in lang_order)

    return {
        "summary": {
            "total_languages": len(languages),
            "total_verses_across_all_languages": total_verses,
            "audio_endpoint_pattern": "/api/v1/{lang}/audio/{book}/{chapter}",
            "audio_info_pattern":     "/api/v1/{lang}/audio/{book}/{chapter}/info",
            "audio_redirect_note": (
                "Audio endpoints return HTTP 307 redirect to the source mp3. "
                "Info endpoint returns HTTP 200 (available) or 404 (not available)."
            ),
        },
        "languages": languages,
    }


@router.get("/coverage/{lang}")
async def get_coverage_for_language(lang: str, db: AsyncSession = Depends(get_db)):
    """
    Text + audio availability for a single language, with per-book breakdown.
    """
    code = lang.lower()
    meta = LANG_STATIC.get(code)
    if not meta:
        raise HTTPException(
            status_code=404,
            detail=f"Language '{code}' not found. Available: {list(LANG_STATIC.keys())}",
        )

    verse_counts = await _verse_counts(db)
    vc = verse_counts.get(code, 0)
    return _build_lang_coverage(code, meta, vc)
