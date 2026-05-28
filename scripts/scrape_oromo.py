"""
Scrape the complete Oromo Bible (Macaafa Qulqulluu) from Bible.com.
Version: MACQUL (3202) — West Central Oromo (Afaan Oromoo).
Source: https://www.bible.com/bible/3202/{BOOK}.{CHAPTER}.MACQUL

Bible.com Terms of Service apply. This data is for personal/dev use.
Contact bible.com for commercial licensing.

Output: data/oromo.json
"""
import json
import re
import time
from pathlib import Path

import httpx
from bs4 import BeautifulSoup

VERSION_ID = 3202
VERSION_TAG = "MACQUL"
BASE_URL = "https://www.bible.com/bible"
OUT_FILE = Path(__file__).parent.parent / "data" / "oromo.json"

# Book abbreviations Bible.com uses (USFM standard)
BOOK_USFM = [
    "GEN", "EXO", "LEV", "NUM", "DEU", "JOS", "JDG", "RUT",
    "1SA", "2SA", "1KI", "2KI", "1CH", "2CH", "EZR", "NEH",
    "EST", "JOB", "PSA", "PRO", "ECC", "SNG", "ISA", "JER",
    "LAM", "EZK", "DAN", "HOS", "JOL", "AMO", "OBA", "JON",
    "MIC", "NAH", "HAB", "ZEP", "HAG", "ZEC", "MAL",
    "MAT", "MRK", "LUK", "JHN", "ACT", "ROM", "1CO", "2CO",
    "GAL", "EPH", "PHP", "COL", "1TH", "2TH", "1TI", "2TI",
    "TIT", "PHM", "HEB", "JAS", "1PE", "2PE", "1JN", "2JN",
    "3JN", "JUD", "REV",
]

CHAPTER_COUNTS = [
    50, 40, 27, 36, 34, 24, 21,  4, 31, 24,
    22, 25, 29, 36, 10, 13, 10, 42, 150, 31,
    12,  8, 66, 52,  5, 48, 12, 14,  3,  9,
     1,  4,  7,  3,  3,  3,  2, 14,  4, 28,
    16, 24, 21, 28, 16, 16, 13,  6,  6,  4,
     4,  5,  3,  6,  4,  3,  1, 13,  5,  5,
     3,  5,  1,  1,  1, 22,
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
}


def parse_chapter_html(html_content: str) -> list[str]:
    """Parse verse texts from the Bible.com chapter HTML snippet."""
    soup = BeautifulSoup(html_content, "lxml")
    verses: dict[int, str] = {}

    for verse_div in soup.find_all(class_=re.compile(r"\bverse\b")):
        usfm = verse_div.get("data-usfm", "")
        # data-usfm = "GEN.1.5" → verse number 5
        parts = usfm.rsplit(".", 1)
        if len(parts) != 2 or not parts[1].isdigit():
            continue
        verse_num = int(parts[1])

        # Remove label spans (verse numbers) from text
        for label in verse_div.find_all(class_="label"):
            label.decompose()
        # Also remove notes/footnotes
        for note in verse_div.find_all(class_=re.compile(r"\bnote\b|\bfootnote\b|\bcrossref\b")):
            note.decompose()

        text = verse_div.get_text(separator=" ").strip()
        text = re.sub(r"\s+", " ", text).strip()

        if text and verse_num > 0:
            # Merge if verse already exists (some translations span multiple divs)
            if verse_num in verses:
                verses[verse_num] += " " + text
            else:
                verses[verse_num] = text

    if not verses:
        return []

    # Return as ordered list; fill gaps with empty string
    max_v = max(verses.keys())
    return [verses.get(i, "").strip() for i in range(1, max_v + 1)]


def fetch_oromo(force: bool = False):
    if OUT_FILE.exists() and not force:
        print(f"[oromo] Cache hit: {OUT_FILE}")
        return

    OUT_FILE.parent.mkdir(exist_ok=True)
    books = []

    with httpx.Client(
        timeout=30,
        follow_redirects=True,
        headers=HEADERS,
    ) as client:
        for book_idx, (usfm, num_chapters) in enumerate(
            zip(BOOK_USFM, CHAPTER_COUNTS), start=1
        ):
            chapters = []
            print(f"[oromo] Book {book_idx:2d}/66 {usfm} — {num_chapters} chapters")

            for ch in range(1, num_chapters + 1):
                url = f"{BASE_URL}/{VERSION_ID}/{usfm}.{ch}.{VERSION_TAG}"
                verses = []

                for attempt in range(4):
                    try:
                        resp = client.get(url)
                        if resp.status_code == 404:
                            print(f"  [404] {url}")
                            break
                        resp.raise_for_status()

                        # Extract the chapter content from __NEXT_DATA__
                        soup = BeautifulSoup(resp.text, "lxml")
                        next_data_tag = soup.find("script", id="__NEXT_DATA__")
                        if next_data_tag:
                            page_data = json.loads(next_data_tag.string)
                            chapter_info = (
                                page_data.get("props", {})
                                .get("pageProps", {})
                                .get("chapterInfo", {})
                            )
                            html_content = chapter_info.get("content", "")
                            verses = parse_chapter_html(html_content)
                        else:
                            # Fallback: parse the rendered HTML directly
                            verses = parse_chapter_html(resp.text)

                        if not verses:
                            print(f"  [warn] No verses at {url}")
                        break

                    except Exception as e:
                        if attempt == 3:
                            print(f"  [error] {url}: {e}")
                        else:
                            time.sleep(2 ** attempt)

                # Remove trailing empty verses
                while verses and not verses[-1]:
                    verses.pop()

                chapters.append(verses)
                time.sleep(0.5)   # respectful rate limit for Bible.com

            books.append({"number": book_idx, "chapters": chapters})

    result = {"books": books}
    OUT_FILE.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    total = sum(len(v) for b in books for ch in b["chapters"] for v in [ch])
    print(f"[oromo] Saved {len(books)} books, {total} verses → {OUT_FILE}")


if __name__ == "__main__":
    fetch_oromo()
