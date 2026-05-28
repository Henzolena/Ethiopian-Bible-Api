"""
Audio Bible router.

Endpoints redirect (307) to free hosted audio sources — no audio stored locally.

Sources discovered:
  Amharic  → j-e-c.org         (full OT + NT, per chapter)
  Oromo    → archive.org       (NT only, per chapter)
  Tigrigna → archive.org       (NT only, per chapter)
  English  → audio.esv.org     (full OT + NT, per chapter, ESV audio)

GET /{lang}/audio/{book}/{chapter}
    → 307 redirect to audio file
    → 404 if not available

GET /{lang}/audio/{book}/{chapter}/info
    → JSON with audio_url, source, coverage info
"""

from fastapi import APIRouter, HTTPException
from fastapi.responses import RedirectResponse

router = APIRouter(tags=["audio"])

# ---------------------------------------------------------------------------
# Book name mappings for j-e-c.org Amharic audio
# Pattern: https://j-e-c.org/site/templates/BibleReading/{testament}/{name}/Ch_{NN}.mp3
# ---------------------------------------------------------------------------

JEC_OT = {
    "GEN": "Genesis",    "EXO": "Exodus",      "LEV": "Leviticus",
    "NUM": "Numbers",    "DEU": "Deuteronomy",  "JOS": "Joshua",
    "JDG": "Judges",     "RUT": "Ruth",         "1SA": "1Samuel",
    "2SA": "2Samuel",    "1KI": "1Kings",        "2KI": "2Kings",
    "1CH": "1Chronicles","2CH": "2Chronicles",   "EZR": "Ezra",
    "NEH": "Nehemiah",   "EST": "Esther",        "JOB": "Job",
    "PSA": "Psalm",      "PRO": "Proverbs",      "ECC": "Ecclesiastes",
    "SNG": "SongOfSongs","ISA": "Isaiah",        "JER": "Jeremiah",
    "LAM": "Lamentations","EZK": "Ezekiel",      "DAN": "Daniel",
    "HOS": "Hosea",      "JOL": "Joel",          "AMO": "Amos",
    "OBA": "Obadiah",    "JON": "Jonah",         "MIC": "Micah",
    "NAH": "Nahum",      "HAB": "Habakkuk",      "ZEP": "Zephaniah",
    "HAG": "Haggai",     "ZEC": "Zechariah",
    # MAL not available on j-e-c.org
}

JEC_NT = {
    "MAT": "Matthew",    "MRK": "Mark",          "JHN": "John",
    "ACT": "Acts",       "ROM": "Romans",         "1CO": "1Corinthians",
    "2CO": "2Corinthians","GAL": "Galatians",     "EPH": "Ephesians",
    "PHP": "Philippians","COL": "Colossians",     "1TH": "1Thessalonians",
    "2TH": "2Thessalonians","1TI": "1Timothy",    "2TI": "2Timothy",
    "TIT": "Titus",      "PHM": "Philemon",       "HEB": "Hebrews",
    "JAS": "James",      "1PE": "1Peter",          "2PE": "2Peter",
    "1JN": "1John",      "2JN": "2John",           "3JN": "3John",
    "JUD": "Jude",       "REV": "Revelation",
    # LUK not available on j-e-c.org
}

# ---------------------------------------------------------------------------
# NT book names for archive.org (Oromo + Tigrigna)
# Pattern: https://archive.org/download/{archive_id}/{bookname}/{NNN}.mp3
# ---------------------------------------------------------------------------

ARCHIVE_NT = {
    "MAT": "matthew",    "MRK": "mark",         "LUK": "luke",
    "JHN": "john",       "ACT": "acts",          "ROM": "romans",
    "1CO": "1corinthians","2CO": "2corinthians", "GAL": "galatians",
    "EPH": "ephesians",  "PHP": "philippians",   "COL": "colossians",
    "1TH": "1thessalonians","2TH":"2thessalonians","1TI":"1timothy",
    "2TI": "2timothy",   "TIT": "titus",          "PHM": "philemon",
    "HEB": "hebrews",    "JAS": "james",          "1PE": "1peter",
    "2PE": "2peter",     "1JN": "1john",          "2JN": "2john",
    "3JN": "3john",      "JUD": "jude",           "REV": "revelation",
}

ARCHIVE_IDS = {
    "or": "bible_Audio_Oromo",
    "ti": "bible_Audio_Amharictigrinya",
}

# ---------------------------------------------------------------------------
# ESV audio uses USFM abbreviations directly
# Pattern: https://audio.esv.org/hw/mq/{ABBR}.{chapter}.mp3
# All 66 books available
# ---------------------------------------------------------------------------

ESV_BOOKS = {
    "GEN","EXO","LEV","NUM","DEU","JOS","JDG","RUT","1SA","2SA",
    "1KI","2KI","1CH","2CH","EZR","NEH","EST","JOB","PSA","PRO",
    "ECC","SNG","ISA","JER","LAM","EZK","DAN","HOS","JOL","AMO",
    "OBA","JON","MIC","NAH","HAB","ZEP","HAG","ZEC","MAL",
    "MAT","MRK","LUK","JHN","ACT","ROM","1CO","2CO","GAL","EPH",
    "PHP","COL","1TH","2TH","1TI","2TI","TIT","PHM","HEB","JAS",
    "1PE","2PE","1JN","2JN","3JN","JUD","REV",
}


# ---------------------------------------------------------------------------
# Core URL builder
# ---------------------------------------------------------------------------

def _build_audio_url(lang: str, abbr: str, chapter: int) -> tuple[str | None, str | None]:
    """
    Returns (url, source_name) or (None, None) if no audio available.
    """
    abbr = abbr.upper()

    if lang == "am":
        if abbr in JEC_OT:
            book_name = JEC_OT[abbr]
            url = (
                f"https://j-e-c.org/site/templates/BibleReading"
                f"/OldTestament/{book_name}/Ch_{chapter:02d}.mp3"
            )
            return url, "Jerusalem Evangelical Church (j-e-c.org)"
        if abbr in JEC_NT:
            book_name = JEC_NT[abbr]
            url = (
                f"https://j-e-c.org/site/templates/BibleReading"
                f"/NewTestament/{book_name}/Ch_{chapter:02d}.mp3"
            )
            return url, "Jerusalem Evangelical Church (j-e-c.org)"
        return None, None

    if lang in ("or", "ti"):
        if abbr in ARCHIVE_NT:
            archive_id = ARCHIVE_IDS[lang]
            book_name = ARCHIVE_NT[abbr]
            url = (
                f"https://archive.org/download/{archive_id}"
                f"/{book_name}/{chapter:03d}.mp3"
            )
            lang_label = "Oromo" if lang == "or" else "Tigrigna"
            return url, f"Internet Archive — {lang_label} NT Audio"
        return None, None  # OT not available for or/ti

    if lang == "en":
        if abbr in ESV_BOOKS:
            url = f"https://audio.esv.org/hw/mq/{abbr}.{chapter}.mp3"
            # NOTE: text is KJV but no free per-chapter KJV audio exists.
            # ESV follows identical chapter/verse structure to KJV but uses
            # modern wording — suitable for listening but not word-sync.
            return url, "ESV Audio Bible (audio.esv.org) — audio is ESV, text is KJV"
        return None, None

    return None, None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/{lang}/audio/{book}/{chapter}/info")
async def audio_info(lang: str, book: str, chapter: int):
    """Return audio URL info as JSON without redirecting.
    Returns 404 (not 200) when audio is unavailable so clients
    can rely on HTTP status alone without parsing the JSON body.
    """
    lang = lang.lower()
    abbr = book.upper()

    url, source = _build_audio_url(lang, abbr, chapter)
    if not url:
        raise HTTPException(
            status_code=404,
            detail={
                "available": False,
                "language": lang,
                "book": abbr,
                "chapter": chapter,
                "audio_url": None,
                "source": None,
                "note": "Audio not available for this language/book combination.",
            },
        )

    # Per-language metadata for iOS clients to handle mismatches gracefully
    LANG_META = {
        "am": {
            "coverage": "Full Bible (OT + NT) — missing Malachi and Luke",
            "text_version": "Amharic Bible (1954/1962 Ethiopian Protestant)",
            "audio_version": "Amharic Bible reading by Paulos Haileselassie (JEC, 2005)",
            "text_audio_match": True,
            "versification": "ethiopian",
            "versification_note": (
                "The Amharic Bible combines some KJV verse pairs into one. "
                "Verse numbers may differ from KJV by 1-2 in some chapters "
                "(e.g. John 3 has 34 verses in Amharic vs 36 in KJV)."
            ),
        },
        "or": {
            "coverage": "New Testament only (27 books, 260 chapters)",
            "text_version": "MACQUL — Macaafa Qulqulluu (Bible Society of Ethiopia)",
            "audio_version": "FCBH Oromo NT (Faith Comes By Hearing)",
            "text_audio_match": True,
            "versification": "standard",
            "versification_note": "Verse numbers match KJV/standard versification.",
        },
        "ti": {
            "coverage": "New Testament only (27 books, 260 chapters)",
            "text_version": "Tigrigna Bible (geezexperience.com)",
            "audio_version": "FCBH Tigrigna NT (Faith Comes By Hearing)",
            "text_audio_match": None,  # unverified — may differ
            "versification": "ethiopian",
            "versification_note": (
                "Tigrigna text combines some verses (e.g. Romans 8 has 32 verses "
                "vs 39 in KJV). Audio translation may differ from text source."
            ),
        },
        "en": {
            "coverage": "Full Bible (OT + NT)",
            "text_version": "King James Version (KJV)",
            "audio_version": "English Standard Version (ESV) — audio.esv.org",
            "text_audio_match": False,
            "versification": "standard",
            "versification_note": (
                "Verse numbers match between KJV and ESV, but wording differs. "
                "No free per-chapter KJV audio exists. Show a disclaimer to users."
            ),
        },
    }

    meta = LANG_META.get(lang, {})
    return {
        "available": True,
        "language": lang,
        "book": abbr,
        "chapter": chapter,
        "audio_url": url,
        "source": source,
        **meta,
    }


@router.get("/{lang}/audio/{book}/{chapter}")
async def audio_redirect(lang: str, book: str, chapter: int):
    """
    307 redirect directly to the audio file URL.
    The client (browser/app) streams audio straight from the source.
    """
    lang = lang.lower()
    abbr = book.upper()

    url, source = _build_audio_url(lang, abbr, chapter)
    if not url:
        raise HTTPException(
            status_code=404,
            detail=(
                f"No audio available for {lang.upper()} {abbr} chapter {chapter}. "
                f"Check /{lang}/audio/{abbr}/{chapter}/info for coverage details."
            ),
        )

    return RedirectResponse(url=url, status_code=307)
