"""
Fetch the Tigrigna Bible from the bible.geezexperience.com JSON API.
The API was discovered by reverse-engineering the Angular SPA at bible.geezexperience.com.
Original text was typed by volunteers; Bible Society of Eritrea holds copyright.
Personal/dev use only — obtain a licence for commercial redistribution.

Output: data/tigrigna.json
"""
import json
import time
from pathlib import Path

import httpx

API_URL = "http://bible.geezexperience.com/server/list_api.php"
OUT_FILE = Path(__file__).parent.parent / "data" / "tigrigna.json"

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
    "User-Agent": "Mozilla/5.0 (compatible; EthiopianBibleAPI/1.0)",
    "Referer": "http://bible.geezexperience.com/",
}


def fetch_tigrigna(force: bool = False):
    if OUT_FILE.exists() and not force:
        print(f"[tigrigna] Cache hit: {OUT_FILE}")
        return

    OUT_FILE.parent.mkdir(exist_ok=True)
    books = []

    with httpx.Client(timeout=30, headers=HEADERS) as client:
        for book_num in range(1, 67):
            chapters = []
            num_chapters = CHAPTER_COUNTS[book_num - 1]
            print(f"[tigrigna] Book {book_num:2d}/66 — {num_chapters} chapters")

            for ch in range(1, num_chapters + 1):
                url = f"{API_URL}?language=tigrigna&book={book_num}&chapter={ch}"
                verses = []

                for attempt in range(4):
                    try:
                        resp = client.get(url)
                        resp.raise_for_status()
                        data = resp.json()

                        # API returns: [{"no": "1", "article": "verse text", ...}, ...]
                        for v in data:
                            text = v.get("article", "").strip()
                            if text:
                                verses.append(text)
                        break

                    except Exception as e:
                        if attempt == 3:
                            print(f"  [error] book={book_num} ch={ch}: {e}")
                        else:
                            time.sleep(2 ** attempt)

                chapters.append(verses)
                time.sleep(0.25)   # respectful rate limit

            books.append({"number": book_num, "chapters": chapters})

    result = {"books": books}
    OUT_FILE.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    total_verses = sum(
        len(v) for b in result["books"] for c in b["chapters"] for v in [c]
    )
    print(f"[tigrigna] Saved {len(books)} books, ~{total_verses} verses → {OUT_FILE}")


if __name__ == "__main__":
    fetch_tigrigna()
