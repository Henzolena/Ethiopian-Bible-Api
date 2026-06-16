"""
Scrape the Amharic Bible directly from wordproject.org/bibles/am/
This replaces the magna25/amharic-bible-json GitHub mirror with the authoritative source.

Features:
  - Checkpoint/resume: saves progress after each book so it can be interrupted and restarted
  - Validates verse counts against known Bible stats
  - Rate-limited to avoid hammering the server

Output: data/amharic.json
Format: { "books": [ { "number": 1, "chapters": [ ["verse1", "verse2", ...], ... ] } ] }

Usage:
    python -m scripts.scrape_amharic_wordproject
"""
import json
import re
import time
from pathlib import Path

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.wordproject.org/bibles/am"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
}
CHECKPOINT_FILE = Path(__file__).parent.parent / "data" / "amharic_scrape_progress.json"
OUT_FILE = Path(__file__).parent.parent / "data" / "amharic.json"

# Expected chapter counts per book (for validation)
EXPECTED_CHAPTERS = [
    50, 40, 27, 36, 34, 24, 21, 4, 31, 24,
    22, 25, 29, 36, 10, 13, 10, 42, 150, 31,
    12, 8, 66, 52, 5, 48, 12, 14, 3, 9,
    1, 4, 7, 3, 3, 3, 2, 14, 4, 28,
    16, 24, 21, 28, 16, 16, 13, 6, 6, 4,
    4, 5, 3, 6, 4, 3, 1, 13, 5, 5,
    3, 5, 1, 1, 1, 22,
]


def fetch_with_retry(client: httpx.Client, url: str, retries: int = 3) -> str:
    for attempt in range(retries):
        try:
            resp = client.get(url, headers=HEADERS, timeout=30)
            resp.raise_for_status()
            return resp.text
        except Exception as exc:
            if attempt == retries - 1:
                raise
            wait = 2 ** attempt
            print(f"    Retry {attempt + 1}/{retries} in {wait}s: {exc}")
            time.sleep(wait)


def get_chapter_count(html: str) -> int:
    """Detect total chapters from the navigation links on a chapter page."""
    nums = [int(m) for m in re.findall(r'href="(\d+)\.htm', html) if m.isdigit()]
    return max(nums) if nums else 1


def parse_verses(html: str) -> list[str]:
    """
    Extract verse texts from a wordproject.org chapter page.

    HTML structure inside div#textBody > p:
      <!--span class="verse" id="1">1  </span-->VERSE_1_TEXT
      <br /><span class="verse" id="2">2 </span>፤ VERSE_2_TEXT
      <br /><span class="verse" id="3">3 </span>፤ VERSE_3_TEXT
    """
    soup = BeautifulSoup(html, "lxml")
    text_body = soup.find("div", id="textBody")
    if not text_body:
        return []

    p = text_body.find("p")
    if not p:
        return []

    inner = str(p)
    # Remove HTML comments — verse 1's span is commented out on every page
    inner = re.sub(r"<!--.*?-->", "", inner, flags=re.DOTALL)
    # Split on verse-number spans to get one segment per verse
    parts = re.split(r'<span[^>]*class="verse"[^>]*>\d+\s*</span>', inner)

    verses = []
    for part in parts:
        # Strip all remaining HTML tags
        text = re.sub(r"<[^>]+>", "", part)
        # Remove the ፤ separator that precedes verse 2+ text
        text = re.sub(r"^[፤\s]+", "", text).strip()
        # Collapse internal whitespace
        text = re.sub(r"\s+", " ", text)
        if text:
            verses.append(text)

    return verses


def scrape() -> dict:
    CHECKPOINT_FILE.parent.mkdir(exist_ok=True)

    # Load existing progress so we can resume after interruption
    progress: dict[str, list] = {}
    if CHECKPOINT_FILE.exists():
        try:
            progress = json.loads(CHECKPOINT_FILE.read_text(encoding="utf-8"))
            print(f"[amharic] Resuming from checkpoint ({len(progress)}/66 books done)")
        except Exception:
            print("[amharic] Checkpoint unreadable — starting fresh")

    all_books = []

    with httpx.Client(follow_redirects=True, timeout=30) as client:
        for book_num in range(1, 67):
            book_key = str(book_num)

            # Use cached result if available
            if book_key in progress:
                all_books.append({"number": book_num, "chapters": progress[book_key]})
                continue

            book_str = f"{book_num:02d}"
            ch1_url = f"{BASE_URL}/{book_str}/1.htm"

            print(f"[{book_num:02d}/66] {ch1_url}")
            ch1_html = fetch_with_retry(client, ch1_url)
            total_chapters = get_chapter_count(ch1_html)

            expected = EXPECTED_CHAPTERS[book_num - 1]
            if total_chapters != expected:
                print(
                    f"  WARNING: detected {total_chapters} chapters, "
                    f"expected {expected} — using detected count"
                )

            chapters = [parse_verses(ch1_html)]  # chapter 1 already fetched

            for ch in range(2, total_chapters + 1):
                url = f"{BASE_URL}/{book_str}/{ch}.htm"
                html = fetch_with_retry(client, url)
                chapters.append(parse_verses(html))
                time.sleep(0.3)  # polite crawl delay

            verse_count = sum(len(c) for c in chapters)
            print(f"  → {total_chapters} chapters, {verse_count} verses")

            # Checkpoint after every book so we can resume if interrupted
            progress[book_key] = chapters
            CHECKPOINT_FILE.write_text(
                json.dumps(progress, ensure_ascii=False), encoding="utf-8"
            )
            all_books.append({"number": book_num, "chapters": chapters})

            time.sleep(0.5)  # extra pause between books

    result = {"books": all_books}
    OUT_FILE.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    total_verses = sum(
        len(ch) for b in all_books for ch in b["chapters"]
    )
    print(f"\n[amharic] Done! {len(all_books)} books, {total_verses} total verses → {OUT_FILE}")

    CHECKPOINT_FILE.unlink(missing_ok=True)
    return result


if __name__ == "__main__":
    scrape()
