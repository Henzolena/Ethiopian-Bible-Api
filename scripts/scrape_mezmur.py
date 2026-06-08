"""
One-time mezmur scraping script.

Scrapes artists, albums, songs, and lyrics from:
  1. onlinemezmur.com  (permissive robots.txt — Crawl-Delay: 20)
  2. wikimezmur.org    (MediaWiki API + HTML; article pages allowed)

Saves to: data/mezmur_data.json  (resumable — appends, skips done items)

Usage:
    python -m scripts.scrape_mezmur [--artists-only] [--no-lyrics] [--limit N]

    --artists-only   Fetch artist + song metadata only (skip lyrics text)
    --no-lyrics      Same as --artists-only
    --limit N        Stop after N artists (for testing)

Run on Railway:
    railway run python -m scripts.scrape_mezmur

Estimated time: ~2-4 hours for full catalogue (rate-limited by crawl delays).
Progress is saved every 50 artists so the script is resumable.
"""
import asyncio
import json
import re
import sys
import argparse
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent.parent))

DATA_FILE = Path(__file__).parent.parent / "data" / "mezmur_data.json"
ONLINE_BASE = "https://onlinemezmur.com"
WIKI_BASE   = "https://wikimezmur.org"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "text/html,application/xhtml+xml,*/*",
    "Accept-Charset": "utf-8",
}

# ─────────────────────────────────────────────────────────────────────────────
# Data structures (saved to JSON)
# ─────────────────────────────────────────────────────────────────────────────

def load_progress() -> dict:
    if DATA_FILE.exists():
        try:
            return json.loads(DATA_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"artists": [], "meta": {"scraped_at": None, "version": 1}}


def save_progress(data: dict):
    import time
    data["meta"]["scraped_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ")
    DATA_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ─────────────────────────────────────────────────────────────────────────────
# HTTP helpers
# ─────────────────────────────────────────────────────────────────────────────

async def fetch_html(client: httpx.AsyncClient, url: str, delay: float = 1.0) -> str | None:
    await asyncio.sleep(delay)
    try:
        r = await client.get(url, headers=HEADERS, timeout=20, follow_redirects=True)
        if r.status_code == 200:
            return r.text
    except Exception as e:
        print(f"    ⚠ fetch error {url}: {e}")
    return None


async def fetch_json(client: httpx.AsyncClient, url: str) -> dict | None:
    await asyncio.sleep(0.5)
    try:
        r = await client.get(url, headers={"Accept": "application/json"}, timeout=20)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        print(f"    ⚠ json fetch error {url}: {e}")
    return None


# ─────────────────────────────────────────────────────────────────────────────
# onlinemezmur.com — artist list
# ─────────────────────────────────────────────────────────────────────────────

def parse_online_artists(html: str) -> list[dict]:
    """Extract artists from search_singer_names.php page."""
    items = []
    # <a href="singer_songs.php?singer_name=ENCODED"><span class="title">Name (COUNT)</span></a>
    pattern = r'href="singer_songs\.php\?singer_name=([^"]+)"[^>]*>.*?<span class="title">\s*([^(<]+)\s*\((\d+)\)'
    for m in re.finditer(pattern, html, re.DOTALL):
        encoded = m.group(1)
        name    = _clean(m.group(2))
        count   = int(m.group(3))
        if name:
            items.append({"name": name, "online_encoded": encoded, "song_count": count})
    return items


async def scrape_online_artists(client: httpx.AsyncClient) -> list[dict]:
    all_artists = []
    page = 0
    print("[online] Fetching artist list…")
    while True:
        url  = f"{ONLINE_BASE}/search_singer_names.php?search=&page={page}"
        html = await fetch_html(client, url, delay=1.0)
        if not html:
            break
        batch = parse_online_artists(html)
        if not batch:
            break
        all_artists.extend(batch)
        print(f"  page {page}: +{len(batch)} artists → total {len(all_artists)}")
        page += 40
    return all_artists


# ─────────────────────────────────────────────────────────────────────────────
# onlinemezmur.com — songs by artist
# ─────────────────────────────────────────────────────────────────────────────

def parse_online_songs(html: str) -> list[dict]:
    songs = []
    # <a href="song_lyrics.php?song_id=ID"><span class="title">Title --- Artist</span>
    pattern = r'href="song_lyrics\.php\?song_id=(\d+)"[^>]*>.*?<span class="title">\s*([^<]+)</span>'
    for m in re.finditer(pattern, html, re.DOTALL):
        song_id = m.group(1)
        raw     = _clean(m.group(2))
        title   = raw.split(" --- ")[0].strip()
        songs.append({"source": "online", "source_id": song_id, "title": title, "album_id": None})
    return songs


async def scrape_online_artist_songs(client: httpx.AsyncClient, encoded: str) -> list[dict]:
    songs, page = [], 0
    while True:
        url  = f"{ONLINE_BASE}/singer_songs.php?singer_name={encoded}&page={page}"
        html = await fetch_html(client, url, delay=0.8)
        if not html:
            break
        batch = parse_online_songs(html)
        if not batch:
            break
        songs.extend(batch)
        page += 10
    return songs


# ─────────────────────────────────────────────────────────────────────────────
# onlinemezmur.com — full lyrics for a song
# ─────────────────────────────────────────────────────────────────────────────

def parse_online_lyrics(html: str) -> str:
    """Extract raw lyrics text from song page (span.versedisplay2)."""
    spans = re.findall(
        r'<span class="versedisplay2"[^>]*>(.*?)</span>',
        html, re.DOTALL
    )
    lines = []
    for span in spans:
        # strip opening id="..." suffix
        content = re.sub(r'^[^>]*>', '', span, count=1)
        text = re.sub(r'<br\s*/?>', '\n', content, flags=re.IGNORECASE)
        text = re.sub(r'<[^>]+>', '', text).strip()
        if text:
            lines.append(text)
    return "\n\n".join(lines)


async def scrape_online_lyrics(client: httpx.AsyncClient, song_id: str) -> str:
    url  = f"{ONLINE_BASE}/song_lyrics.php?song_id={song_id}"
    html = await fetch_html(client, url, delay=0.5)
    return parse_online_lyrics(html) if html else ""


# ─────────────────────────────────────────────────────────────────────────────
# wikimezmur.org — artist list (MediaWiki API)
# ─────────────────────────────────────────────────────────────────────────────

async def scrape_wiki_artists(client: httpx.AsyncClient) -> list[dict]:
    artists, cont = [], None
    print("[wiki] Fetching artist list via MediaWiki API…")
    while True:
        url = f"{WIKI_BASE}/api.php?action=query&list=allpages&aplimit=500&format=json&apnamespace=0"
        if cont:
            url += f"&apcontinue={cont}"
        data = await fetch_json(client, url)
        if not data:
            break
        for p in data.get("query", {}).get("allpages", []):
            title = p.get("title", "")
            if _is_artist_page(title):
                path = title.replace(" ", "_")
                artists.append({"name": title, "wiki_path": path})
        cont = data.get("continue", {}).get("apcontinue")
        if not cont:
            break
    print(f"  {len(artists)} wiki artist pages found")
    return artists


def _is_artist_page(title: str) -> bool:
    if ":" in title:
        return False
    skip = {"Home", "Main Page", "Sandbox", "Welcome", "Help", "About"}
    if title in skip:
        return False
    if len(title) < 2:
        return False
    first = title[0]
    return first.isalpha() or ('ሀ' <= first <= '፿')


# ─────────────────────────────────────────────────────────────────────────────
# wikimezmur.org — artist page (Albums + Tracks)
# ─────────────────────────────────────────────────────────────────────────────

def parse_wiki_artist_page(html: str, wiki_path: str) -> tuple[list[dict], list[dict]]:
    """Return (albums, songs) from an artist page."""
    # Track links follow /am/Artist/Album/Track pattern
    # Group by album slug
    albums_dict: dict[str, dict] = {}
    seen_track_ids = set()

    pattern = r'href="(/am/[^/]+/([^/]+)/([^"#?]+))"'
    for m in re.finditer(pattern, html):
        full_path  = m.group(1)
        album_slug = _clean_slug(m.group(2))
        track_slug = _clean_slug(m.group(3))

        track_id = f"wiki_{full_path}"
        if track_id in seen_track_ids:
            continue
        seen_track_ids.add(track_id)

        album_title = album_slug.replace("_", " ").replace("-", " ")
        track_title = track_slug.replace("_", " ").replace("-", " ")

        if album_slug not in albums_dict:
            albums_dict[album_slug] = {
                "title":     album_title,
                "wiki_slug": album_slug,
                "tracks":    [],
            }
        albums_dict[album_slug]["tracks"].append({
            "source":    "wiki",
            "source_id": full_path,
            "title":     track_title,
            "lyrics":    "",
        })

    albums = list(albums_dict.values())
    # Also extract any direct song links (2-level: /am/Artist/Song)
    flat_songs = []
    flat_pattern = r'href="(/am/' + re.escape(wiki_path) + r'/([^/"#?]+))"'
    flat_seen = {t["source_id"] for a in albums for t in a["tracks"]}
    for m in re.finditer(flat_pattern, html):
        full = m.group(1)
        if full not in flat_seen:
            flat_seen.add(full)
            flat_songs.append({
                "source":    "wiki",
                "source_id": full,
                "title":     _clean_slug(m.group(2)).replace("_", " "),
                "album_id":  None,
                "lyrics":    "",
            })

    return albums, flat_songs


async def scrape_wiki_lyrics(client: httpx.AsyncClient, wiki_path: str) -> str:
    """Fetch lyrics text from a wikimezmur article page."""
    url  = f"{WIKI_BASE}{wiki_path}"
    html = await fetch_html(client, url, delay=0.3)
    if not html:
        return ""
    # Extract content inside mw-content-text
    content_m = re.search(r'id="mw-content-text".*?<div[^>]*>(.*?)</div>\s*</div>',
                           html, re.DOTALL)
    content = content_m.group(1) if content_m else html
    # Get paragraphs and pre blocks
    texts = []
    for tag in ("p", "pre"):
        for m in re.finditer(rf"<{tag}>(.*?)</{tag}>", content, re.DOTALL):
            text = re.sub(r'<br\s*/?>', '\n', m.group(1), flags=re.IGNORECASE)
            text = re.sub(r'<[^>]+>', '', text).strip()
            if text and len(text) > 5:
                texts.append(text)
    return "\n\n".join(texts)


# ─────────────────────────────────────────────────────────────────────────────
# Deduplication & merging
# ─────────────────────────────────────────────────────────────────────────────

def normalise(name: str) -> str:
    return " ".join(name.lower().replace("_", " ").split())


def merge_artists(
    online: list[dict],
    wiki:   list[dict],
) -> list[dict]:
    """Merge online + wiki artists; deduplicate by normalised name."""
    by_key: dict[str, dict] = {}

    for a in online:
        key = normalise(a["name"])
        by_key[key] = {
            "name":           a["name"],
            "name_normalized": key,
            "source":         "online",
            "online_encoded": a["online_encoded"],
            "wiki_path":      None,
            "song_count":     a.get("song_count", 0),
            "songs":          a.get("songs", []),
            "albums":         [],
        }

    for a in wiki:
        key = normalise(a["name"])
        if key in by_key:
            by_key[key]["source"]    = "both"
            by_key[key]["wiki_path"] = a["wiki_path"]
            by_key[key]["albums"]    = a.get("albums", [])
            # Merge wiki songs that aren't already in online songs
            existing_ids = {s["source_id"] for s in by_key[key]["songs"]}
            for s in a.get("songs", []):
                if s["source_id"] not in existing_ids:
                    by_key[key]["songs"].append(s)
        else:
            by_key[key] = {
                "name":           a["name"],
                "name_normalized": key,
                "source":         "wiki",
                "online_encoded": None,
                "wiki_path":      a["wiki_path"],
                "song_count":     len(a.get("songs", [])),
                "songs":          a.get("songs", []),
                "albums":         a.get("albums", []),
            }

    return sorted(by_key.values(), key=lambda x: x["name"])


# ─────────────────────────────────────────────────────────────────────────────
# Main scraping orchestrator
# ─────────────────────────────────────────────────────────────────────────────

async def main(args):
    data = load_progress()
    existing_names = {normalise(a["name"]) for a in data["artists"]}

    async with httpx.AsyncClient(timeout=30) as client:

        # ── Step 1: Online artist list ────────────────────────────────────────
        print("\n═══ Step 1: Scrape onlinemezmur.com artist list ═══")
        online_artists = await scrape_online_artists(client)

        # ── Step 2: Wiki artist list ──────────────────────────────────────────
        print("\n═══ Step 2: Scrape wikimezmur.org artist list ═══")
        wiki_artists_raw = await scrape_wiki_artists(client)

        # ── Step 3: Merge ─────────────────────────────────────────────────────
        print("\n═══ Step 3: Merge + deduplicate ═══")
        # Temporarily attach empty songs/albums for merge
        for a in online_artists:
            a["songs"]  = []
            a["albums"] = []
        for a in wiki_artists_raw:
            a["songs"]  = []
            a["albums"] = []

        merged = merge_artists(online_artists, wiki_artists_raw)
        print(f"  {len(merged)} unique artists after dedup")

        limit = args.limit or len(merged)
        merged = merged[:limit]

        # ── Step 4: Per-artist songs + lyrics ────────────────────────────────
        print(f"\n═══ Step 4: Fetch songs for {len(merged)} artists ═══")
        for i, artist in enumerate(merged):
            key = artist["name_normalized"]
            if key in existing_names:
                print(f"  [{i+1}/{len(merged)}] SKIP (already done): {artist['name']}")
                continue

            print(f"  [{i+1}/{len(merged)}] {artist['name']} ({artist['source']})")

            # Online songs
            if artist["online_encoded"]:
                songs = await scrape_online_artist_songs(client, artist["online_encoded"])
                artist["songs"].extend(songs)
                artist["song_count"] = len(songs)
                print(f"    → {len(songs)} songs from onlinemezmur")

            # Wiki albums + songs
            if artist["wiki_path"]:
                url  = f"{WIKI_BASE}/am/{artist['wiki_path']}"
                html = await fetch_html(client, url, delay=0.5)
                if html:
                    albums, flat_songs = parse_wiki_artist_page(html, artist["wiki_path"])
                    artist["albums"] = albums
                    # Add track songs from albums
                    for album in albums:
                        for track in album.get("tracks", []):
                            # Check not already in online songs
                            if not any(s["source_id"] == track["source_id"] for s in artist["songs"]):
                                artist["songs"].append({
                                    "source":    track["source"],
                                    "source_id": track["source_id"],
                                    "title":     track["title"],
                                    "album_id":  None,  # resolved at seed time
                                    "lyrics":    "",
                                })
                    artist["songs"].extend(flat_songs)
                    print(f"    → {len(albums)} albums, {sum(len(a['tracks']) for a in albums)} tracks from wiki")

            # Lyrics (if not --artists-only)
            if not args.artists_only:
                lyrics_fetched = 0
                for song in artist["songs"]:
                    if song.get("lyrics"):
                        continue
                    if song["source"] == "online":
                        lyrics = await scrape_online_lyrics(client, song["source_id"])
                    else:  # wiki
                        lyrics = await scrape_wiki_lyrics(client, song["source_id"])
                    song["lyrics"] = lyrics
                    if lyrics:
                        lyrics_fetched += 1
                print(f"    → {lyrics_fetched}/{len(artist['songs'])} lyrics fetched")

            data["artists"].append(artist)
            existing_names.add(key)

            # Save progress every 50 artists
            if (i + 1) % 50 == 0:
                save_progress(data)
                print(f"  💾 Progress saved ({len(data['artists'])} artists)")

    save_progress(data)
    total_songs  = sum(len(a["songs"])  for a in data["artists"])
    total_albums = sum(len(a["albums"]) for a in data["artists"])
    print(f"\n✅ Done! {len(data['artists'])} artists, {total_albums} albums, {total_songs} songs")
    print(f"   Saved to: {DATA_FILE}")


# ─────────────────────────────────────────────────────────────────────────────
# Utilities
# ─────────────────────────────────────────────────────────────────────────────

def _clean(s: str) -> str:
    s = re.sub(r'<[^>]+>', ' ', s)
    s = s.replace("&amp;", "&").replace("&nbsp;", " ").replace("&quot;", '"')
    return " ".join(s.split())


def _clean_slug(s: str) -> str:
    return s.split("#")[0].split("?")[0].rstrip("/")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Scrape mezmur data from onlinemezmur.com + wikimezmur.org")
    parser.add_argument("--artists-only", "--no-lyrics", action="store_true",
                        help="Fetch artist/song metadata only (skip lyrics text)")
    parser.add_argument("--limit", type=int, default=None,
                        help="Max number of artists to process (for testing)")
    args = parser.parse_args()
    asyncio.run(main(args))
