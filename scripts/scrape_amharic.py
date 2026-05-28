"""
Download and normalize the Amharic Bible from magna25/amharic-bible-json (GitHub).
Source: https://github.com/magna25/amharic-bible-json
Original text from wordproject.org/bibles/am/ with gaps filled from bible.geezexperience.com/amharic/

Output: data/amharic.json
Format: { "books": [ { "number": 1, "chapters": [ ["verse1", "verse2", ...], ... ] } ] }
"""
import json
import time
import httpx
from pathlib import Path

RAW_URL = "https://raw.githubusercontent.com/magna25/amharic-bible-json/main/amharic_bible.json"
OUT_FILE = Path(__file__).parent.parent / "data" / "amharic.json"

BOOK_ORDER = [
    "Genesis", "Exodus", "Leviticus", "Numbers", "Deuteronomy",
    "Joshua", "Judges", "Ruth", "1 Samuel", "2 Samuel",
    "1 Kings", "2 Kings", "1 Chronicles", "2 Chronicles", "Ezra",
    "Nehemiah", "Esther", "Job", "Psalms", "Proverbs",
    "Ecclesiastes", "Song of Solomon", "Isaiah", "Jeremiah", "Lamentations",
    "Ezekiel", "Daniel", "Hosea", "Joel", "Amos",
    "Obadiah", "Jonah", "Micah", "Nahum", "Habakkuk",
    "Zephaniah", "Haggai", "Zechariah", "Malachi",
    "Matthew", "Mark", "Luke", "John", "Acts",
    "Romans", "1 Corinthians", "2 Corinthians", "Galatians", "Ephesians",
    "Philippians", "Colossians", "1 Thessalonians", "2 Thessalonians",
    "1 Timothy", "2 Timothy", "Titus", "Philemon", "Hebrews",
    "James", "1 Peter", "2 Peter", "1 John", "2 John",
    "3 John", "Jude", "Revelation",
]


def fetch_amharic():
    if OUT_FILE.exists():
        print(f"[amharic] Cache hit: {OUT_FILE}")
        return

    print(f"[amharic] Downloading from GitHub... {RAW_URL}")
    with httpx.Client(timeout=60, follow_redirects=True) as client:
        resp = client.get(RAW_URL)
        resp.raise_for_status()

    raw = resp.json()

    # magna25 format:
    # { "title": "...", "books": [ { "title": "Genesis", "abbv": "gen",
    #   "chapters": [ { "chapter": "1", "title": "", "verses": ["v1", ...] }, ... ]
    # }, ... ] }
    if isinstance(raw, dict) and "books" in raw:
        books_raw = raw["books"]
    elif isinstance(raw, list):
        books_raw = raw
    else:
        raise ValueError("Unexpected JSON structure from amharic-bible-json")

    normalized = {"books": []}
    for i, book_data in enumerate(books_raw, start=1):
        if isinstance(book_data, dict):
            chapters_raw = book_data.get("chapters", [])
        else:
            chapters_raw = book_data

        chapters = []
        for chapter in chapters_raw:
            if isinstance(chapter, dict):
                # { "chapter": "1", "verses": [...] }
                verses_raw = chapter.get("verses", [])
            elif isinstance(chapter, list):
                verses_raw = chapter
            else:
                verses_raw = [chapter]

            if isinstance(verses_raw, list):
                verses = [str(v).strip() for v in verses_raw if str(v).strip()]
            else:
                verses = [str(verses_raw).strip()]

            chapters.append(verses)

        normalized["books"].append({"number": i, "chapters": chapters})

    OUT_FILE.parent.mkdir(exist_ok=True)
    OUT_FILE.write_text(json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[amharic] Saved {len(normalized['books'])} books → {OUT_FILE}")


if __name__ == "__main__":
    fetch_amharic()
