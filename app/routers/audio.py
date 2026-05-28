"""
Audio Bible router.

Endpoints redirect (307) to free hosted audio sources — no audio stored locally.

Sources discovered:
  Amharic  → j-e-c.org            (full OT + NT, per chapter)
  Oromo    → archive.org          (NT only, per chapter)
  Tigrigna → archive.org          (NT only, per chapter)
  English  → audiotreasure.com    (full OT + NT, per chapter, KJV — exact text match)
  NIV      → archive.org          (full OT + NT, per chapter — exact NIV text match)

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
# NIV audio — archive.org "englishNIVAudioBible" collection
# Pattern: https://archive.org/download/englishNIVAudioBible/englishniv/{name}/{NNN}.mp3
# Full 66 books OT + NT, per chapter — exact match with bolls.life NIV text
# ---------------------------------------------------------------------------

NIV_AUDIO_BOOKS = {
    "GEN": "genesis",        "EXO": "exodus",          "LEV": "leviticus",
    "NUM": "numbers",        "DEU": "deuteronomy",      "JOS": "joshua",
    "JDG": "judges",         "RUT": "ruth",             "1SA": "1samuel",
    "2SA": "2samuel",        "1KI": "1kings",            "2KI": "2kings",
    "1CH": "1chronicles",    "2CH": "2chronicles",       "EZR": "ezra",
    "NEH": "nehemiah",       "EST": "esther",            "JOB": "job",
    "PSA": "psalms",         "PRO": "proverbs",          "ECC": "ecclesiastes",
    "SNG": "songOfSolomon",  "ISA": "isaiah",            "JER": "jeremiah",
    "LAM": "lamentations",   "EZK": "ezekiel",           "DAN": "daniel",
    "HOS": "hosea",          "JOL": "joel",              "AMO": "amos",
    "OBA": "obadiah",        "JON": "jonah",             "MIC": "micah",
    "NAH": "nahum",          "HAB": "habakkuk",          "ZEP": "zephaniah",
    "HAG": "haggai",         "ZEC": "zechariah",         "MAL": "malachi",
    "MAT": "matthew",        "MRK": "mark",              "LUK": "luke",
    "JHN": "john",           "ACT": "acts",              "ROM": "romans",
    "1CO": "1corinthians",   "2CO": "2corinthians",      "GAL": "galatians",
    "EPH": "ephesians",      "PHP": "philippians",       "COL": "colossians",
    "1TH": "1thessalonians", "2TH": "2thessalonians",    "1TI": "1timothy",
    "2TI": "2timothy",       "TIT": "titus",             "PHM": "philemon",
    "HEB": "hebrews",        "JAS": "james",             "1PE": "1peter",
    "2PE": "2peter",         "1JN": "1john",             "2JN": "2john",
    "3JN": "3john",          "JUD": "jude",              "REV": "revelation",
}

# ---------------------------------------------------------------------------
# AudioTreasure KJV — exact match with our KJV text
# Pattern: https://audiotreasure.com/content/KJV_AT/{NN}_{BookName}{chapter:03d}.mp3
# All 66 books, full OT + NT, per chapter — King James Version (voice only)
# Source: https://audiotreasure.com (free, non-commercial use)
# ---------------------------------------------------------------------------

AUDIOTREASURE_KJV = {
    "GEN": "01_Genesis",     "EXO": "02_Exodus",        "LEV": "03_Leviticus",
    "NUM": "04_Numbers",     "DEU": "05_Deuteronomy",   "JOS": "06_Joshua",
    "JDG": "07_Judges",      "RUT": "08_Ruth",           "1SA": "09_1Samuel",
    "2SA": "10_2Samuel",     "1KI": "11_1Kings",         "2KI": "12_2Kings",
    "1CH": "13_1Chronicles", "2CH": "14_2Chronicles",    "EZR": "15_Ezra",
    "NEH": "16_Nehemiah",    "EST": "17_Esther",         "JOB": "18Job",
    "PSA": "19_Psalms",      "PRO": "20_Proverbs",       "ECC": "21_Ecclesiastes",
    "SNG": "22_Song_of_Solomon", "ISA": "23_Isaiah",     "JER": "24_Jeremiah",
    "LAM": "25_Lamentations","EZK": "26_Ezekiel",        "DAN": "27_Daniel",
    "HOS": "28_Hosea",       "JOL": "29_Joel",           "AMO": "30_Amos",
    "OBA": "31_Obadiah",     "JON": "32_Jonah",          "MIC": "33_Micah",
    "NAH": "34_Nahum",       "HAB": "35_Habakkuk",       "ZEP": "36_Zephaniah",
    "HAG": "37_Haggai",      "ZEC": "38_Zechariah",      "MAL": "39_Malachi",
    "MAT": "40_Matthew",     "MRK": "41_Mark",           "LUK": "42_Luke",
    "JHN": "43_John",        "ACT": "44_Acts",           "ROM": "45_Romans",
    "1CO": "46_1Corinthians","2CO": "47_2Corinthians",   "GAL": "48_Galatians",
    "EPH": "49_Ephesians",   "PHP": "50_Philippians",    "COL": "51_Colossians",
    "1TH": "52_1Thessalonians","2TH":"53_2Thessalonians","1TI": "54_1Timothy",
    "2TI": "55_2Timothy",    "TIT": "56_Titus",          "PHM": "57_Philemon",
    "HEB": "58_Hebrews",     "JAS": "59_James",          "1PE": "60_1Peter",
    "2PE": "61_2Peter",      "1JN": "62_1John",          "2JN": "63_2John",
    "3JN": "64_3John",       "JUD": "65_Jude",           "REV": "66_Revelation",
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
        if abbr in AUDIOTREASURE_KJV:
            book_name = AUDIOTREASURE_KJV[abbr]
            url = f"https://audiotreasure.com/content/KJV_AT/{book_name}{chapter:03d}.mp3"
            return url, "AudioTreasure KJV (audiotreasure.com) — exact KJV text match"
        return None, None

    if lang == "niv":
        if abbr in NIV_AUDIO_BOOKS:
            book_name = NIV_AUDIO_BOOKS[abbr]
            url = (
                f"https://archive.org/download/englishNIVAudioBible"
                f"/englishniv/{book_name}/{chapter:03d}.mp3"
            )
            return url, "Internet Archive — English NIV Audio Bible (exact NIV text match)"
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
            "coverage": "Full Bible (OT + NT) — all 66 books",
            "text_version": "King James Version (KJV)",
            "audio_version": "King James Version (KJV) — AudioTreasure.com, voice only",
            "text_audio_match": True,
            "versification": "standard",
            "versification_note": "Verse numbers and wording match exactly between text and audio.",
        },
        "niv": {
            "coverage": "Full Bible (OT + NT) — all 66 books",
            "text_version": "New International Version (NIV) — Biblica, 2011",
            "audio_version": "NIV Audio Bible — Internet Archive (englishNIVAudioBible)",
            "text_audio_match": True,
            "versification": "standard",
            "versification_note": "NIV verse numbers match standard versification. Text and audio are both NIV 2011 edition.",
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
