"""
Download the English KJV Bible from thiagobodruk/bible (GitHub).
Source: https://github.com/thiagobodruk/bible/raw/master/json/en_kjv.json
Public domain text.

Output: data/english.json
"""
import json
import httpx
from pathlib import Path

RAW_URL = "https://raw.githubusercontent.com/thiagobodruk/bible/master/json/en_kjv.json"
OUT_FILE = Path(__file__).parent.parent / "data" / "english.json"


def fetch_english(force: bool = False):
    if OUT_FILE.exists() and not force:
        print(f"[english] Cache hit: {OUT_FILE}")
        return

    print(f"[english] Downloading KJV from GitHub...")
    with httpx.Client(timeout=60, follow_redirects=True) as client:
        resp = client.get(RAW_URL)
        resp.raise_for_status()

    raw = resp.json()

    # thiagobodruk format:
    # [ { "abbrev": "gn", "chapters": [ ["In the beginning...", ...], ... ] }, ... ]
    normalized = {"books": []}
    for i, book_data in enumerate(raw, start=1):
        chapters_raw = book_data.get("chapters", [])
        chapters = []
        for chap in chapters_raw:
            if isinstance(chap, list):
                verses = [str(v).strip() for v in chap]
            else:
                verses = [str(chap).strip()]
            chapters.append(verses)
        normalized["books"].append({"number": i, "chapters": chapters})

    OUT_FILE.parent.mkdir(exist_ok=True)
    OUT_FILE.write_text(json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[english] Saved {len(normalized['books'])} books → {OUT_FILE}")


if __name__ == "__main__":
    fetch_english()
