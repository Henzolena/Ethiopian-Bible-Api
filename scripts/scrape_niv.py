"""
Download the English NIV Bible from bolls.life public API.
Source: https://bolls.life/get-chapter/NIV/{book}/{chapter}/
Returns JSON array of {pk, verse, text} per chapter.

Note: NIV text © Biblica. This uses bolls.life as a public aggregator.
      Full 66-book coverage confirmed.

Output: data/niv.json
"""
import re
import time
import json
import httpx
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))
from scripts.bible_books import BOOKS

OUT_FILE = Path(__file__).parent.parent / "data" / "niv.json"
BASE_URL = "https://bolls.life/get-chapter/NIV/{book}/{chapter}/"

# Delay between requests to avoid rate limiting
REQUEST_DELAY = 0.25  # seconds

_HTML_TAG = re.compile(r"<[^>]+>")


def _clean(text: str) -> str:
    """
    Strip bolls.life HTML artifacts from NIV verse text.
    bolls.life injects section headings as: 'Heading Text<br/>Actual verse...'
    We keep only the text AFTER the last <br/> tag (the actual verse).
    If there is no <br/>, strip any remaining tags.
    """
    # Split on <br/> variants and take the last non-empty segment
    parts = re.split(r"<br\s*/?>", text, flags=re.IGNORECASE)
    # Last segment is the real verse text
    verse = parts[-1] if parts else text
    # Strip any remaining HTML tags
    verse = _HTML_TAG.sub("", verse)
    return verse.strip()


def fetch_niv(force: bool = False):
    if OUT_FILE.exists() and not force:
        print(f"[niv] Cache hit: {OUT_FILE}")
        return

    print("[niv] Fetching NIV text from bolls.life API...")
    normalized = {"books": []}

    with httpx.Client(timeout=30, follow_redirects=True) as client:
        for book_row in BOOKS:
            book_num, abbr, eng_name, testament, chapter_count, *_ = book_row
            chapters = []
            print(f"  [{book_num:2d}/66] {eng_name} ({chapter_count} chapters)...", end="", flush=True)

            for ch in range(1, chapter_count + 1):
                url = BASE_URL.format(book=book_num, chapter=ch)
                for attempt in range(3):
                    try:
                        resp = client.get(url)
                        resp.raise_for_status()
                        break
                    except httpx.HTTPError as e:
                        if attempt == 2:
                            print(f"\n[niv] ERROR fetching {eng_name} {ch}: {e}")
                            chapters.append([])
                            continue
                        time.sleep(1)

                verses_json = resp.json()
                # bolls.life returns: [{"pk": N, "verse": N, "text": "..."}, ...]
                # Text may include HTML tags like <br/> for section headings — strip them.
                verses = [_clean(v["text"]) for v in sorted(verses_json, key=lambda x: x["verse"])]
                chapters.append(verses)
                time.sleep(REQUEST_DELAY)

            normalized["books"].append({"number": book_num, "chapters": chapters})
            print(f" ✓ {sum(len(c) for c in chapters)} verses")

    OUT_FILE.parent.mkdir(exist_ok=True)
    OUT_FILE.write_text(json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8")
    total_verses = sum(
        len(v) for b in normalized["books"] for c in b["chapters"] for v in [c]
    )
    print(f"[niv] Saved {len(normalized['books'])} books, ~{total_verses} verses → {OUT_FILE}")


if __name__ == "__main__":
    fetch_niv(force="--force" in sys.argv)
