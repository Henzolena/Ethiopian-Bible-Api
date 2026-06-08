"""
Robust mezmur scraping script — complete rewrite.

Sources:
  onlinemezmur.com  — artist list via search_singer_names.php (paginated)
                      songs via singer_songs.php, lyrics via song_lyrics.php
  wikimezmur.org    — SITEMAP.XML for clean hierarchy (not MediaWiki allpages)
                      Sitemap gives Artist / Album / Track in path structure

Key improvements over v1:
  - Wiki: uses sitemap.xml (real artist pages, not 6,625 spam API pages)
  - URL extraction: strict regex, no whitespace/HTML allowed in URLs
  - HTML parsing: BeautifulSoup instead of fragile raw regex
  - Artist filtering: rejects English-sentence spam, non-music pages
  - Resumable: saves every 100 artists, skips existing
  - Clean deduplication with normalised names
  - BeautifulSoup-based lyrics extraction (accurate, handles <br>)

Usage:
    python -m scripts.scrape_mezmur               # full run
    python -m scripts.scrape_mezmur --limit 50    # test with 50 artists
    python -m scripts.scrape_mezmur --artists-only # metadata only, no lyrics
    python -m scripts.scrape_mezmur --wiki-only    # only wikimezmur
    python -m scripts.scrape_mezmur --online-only  # only onlinemezmur

On Railway:
    railway run python -m scripts.scrape_mezmur --limit 200 --artists-only
    # (then run seed once satisfied, then full lyrics run)
"""
import asyncio
import json
import re
import sys
import time
import argparse
import unicodedata
from pathlib import Path
from xml.etree import ElementTree

import httpx
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).parent.parent))

# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────

DATA_FILE   = Path(__file__).parent.parent / "data" / "mezmur_data.json"
ONLINE_BASE = "https://onlinemezmur.com"
WIKI_BASE   = "https://wikimezmur.org"

# Per-request delays (seconds) — be a respectful crawler
DELAYS = {
    "online_artist_list": 0.8,
    "online_song_list":   0.5,
    "online_lyrics":      0.4,
    "wiki_sitemap":       1.0,
    "wiki_lyrics":        0.3,
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Safari/537.36",
    "Accept":     "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
    "Accept-Encoding": "gzip, deflate",
    "Accept-Language": "am,en;q=0.5",
}

# ─────────────────────────────────────────────────────────────────────────────
# Persistence
# ─────────────────────────────────────────────────────────────────────────────

def load_progress() -> dict:
    if DATA_FILE.exists():
        try:
            d = json.loads(DATA_FILE.read_text(encoding="utf-8"))
            print(f"  Resuming from saved progress: {len(d.get('artists', []))} artists already done")
            return d
        except Exception:
            pass
    return {"artists": [], "meta": {}}


def save_progress(data: dict):
    data["meta"]["scraped_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ")
    DATA_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ─────────────────────────────────────────────────────────────────────────────
# HTTP helpers
# ─────────────────────────────────────────────────────────────────────────────

async def fetch_html(client: httpx.AsyncClient, url: str, delay: float = 0.5) -> str | None:
    """Fetch a URL and return HTML string, or None on failure."""
    if delay:
        await asyncio.sleep(delay)
    try:
        r = await client.get(url, headers=HEADERS, timeout=25, follow_redirects=True)
        if r.status_code == 200:
            # Detect encoding
            text = r.text
            return text
        else:
            print(f"  HTTP {r.status_code}: {url[:80]}")
    except Exception as e:
        # Only print short error, not huge URL+HTML fragments
        print(f"  ⚠ {type(e).__name__}: {str(e)[:100]}")
    return None


async def fetch_xml(client: httpx.AsyncClient, url: str, delay: float = 1.0) -> str | None:
    if delay:
        await asyncio.sleep(delay)
    try:
        r = await client.get(url, headers={**HEADERS, "Accept": "application/xml,text/xml"}, timeout=30)
        if r.status_code == 200:
            return r.text
    except Exception as e:
        print(f"  ⚠ XML fetch {type(e).__name__}: {str(e)[:100]}")
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Name normalisation & validation
# ─────────────────────────────────────────────────────────────────────────────

def normalise_name(name: str) -> str:
    """Lowercase, collapse whitespace/underscores for dedup comparison."""
    name = name.replace("_", " ").replace("-", " ")
    name = " ".join(name.lower().split())
    return name


def is_likely_person_name(name: str) -> bool:
    """
    Returns True if the page title looks like a real artist name.
    Rejects:
      - Long English sentences (spam pages like "A Day Trading Strategy...")
      - Known navigation page names
      - Names with special chars that indicate non-person pages
    """
    name = name.strip()
    if not name or len(name) < 2:
        return False

    # Navigation/meta pages
    SKIP_EXACT = {
        "Home", "Main Page", "Sandbox", "Welcome", "Privacy", "About",
        "Disclaimers", "Church_Directory", "SubmitList", "Submit",
        "Oromiffa", "Tigrinya", "Classics", "Browse", "Special",
        "MediaWiki", "Talk", "User", "File", "Category", "Help",
    }
    for skip in SKIP_EXACT:
        if name.lower() == skip.lower():
            return False

    # Contains colon (MediaWiki special pages)
    if ":" in name:
        return False

    # Long English sentences (spam) — heuristic: > 7 words AND all ASCII
    words = name.split()
    all_ascii = all(ord(c) < 128 for c in name)
    if len(words) > 7 and all_ascii:
        return False

    # Sentence-like English — starts with "A " or "The " and > 5 words
    if all_ascii and len(words) > 5 and words[0].lower() in ("a", "an", "the", "how", "why", "what", "tips"):
        return False

    # Must start with a letter (Latin or Ethiopic)
    first_char = name[0]
    if not (first_char.isalpha() or 'ሀ' <= first_char <= '፿'):
        return False

    return True


# ─────────────────────────────────────────────────────────────────────────────
# ══ SOURCE 1: onlinemezmur.com ══
# ─────────────────────────────────────────────────────────────────────────────

async def scrape_online_artists(client: httpx.AsyncClient) -> list[dict]:
    """
    Fetch all artists from search_singer_names.php (40 per page).
    Returns list of {"name", "online_encoded", "song_count"}.
    """
    artists = []
    page    = 0

    print("[onlinemezmur] Fetching artist list…")
    while True:
        url  = f"{ONLINE_BASE}/search_singer_names.php?search=&page={page}"
        html = await fetch_html(client, url, delay=DELAYS["online_artist_list"])
        if not html:
            break

        soup   = BeautifulSoup(html, "html.parser")
        # Each artist: <a href="singer_songs.php?singer_name=ENCODED"><span class="title">Name (N)</span></a>
        links  = soup.find_all("a", href=re.compile(r"singer_songs\.php\?singer_name="))
        if not links:
            break

        batch = []
        for a in links:
            href  = a.get("href", "")
            span  = a.find("span", class_="title")
            if not span:
                continue
            text  = span.get_text(strip=True)
            # "Artist Name (12)"
            m = re.match(r"^(.+?)\s*\((\d+)\)\s*$", text)
            if not m:
                continue
            name  = m.group(1).strip()
            count = int(m.group(2))
            enc   = re.search(r"singer_name=(.+?)$", href)
            if not enc or not name:
                continue
            batch.append({
                "name":           name,
                "online_encoded": enc.group(1),
                "song_count":     count,
            })

        artists.extend(batch)
        print(f"  page={page}: +{len(batch)} artists → {len(artists)} total")
        if not batch:
            break
        page += 40

    print(f"  ✓ {len(artists)} artists from onlinemezmur.com")
    return artists


async def scrape_online_songs(client: httpx.AsyncClient, encoded_name: str) -> list[dict]:
    """Fetch all songs for an artist from singer_songs.php."""
    songs = []
    page  = 0
    while True:
        url  = f"{ONLINE_BASE}/singer_songs.php?singer_name={encoded_name}&page={page}"
        html = await fetch_html(client, url, delay=DELAYS["online_song_list"])
        if not html:
            break

        soup  = BeautifulSoup(html, "html.parser")
        links = soup.find_all("a", href=re.compile(r"song_lyrics\.php\?song_id=\d+"))
        if not links:
            break

        batch = []
        for a in links:
            href  = a.get("href", "")
            span  = a.find("span", class_="title")
            if not span:
                continue
            m = re.search(r"song_id=(\d+)", href)
            if not m:
                continue
            title = span.get_text(strip=True).split(" --- ")[0].strip()
            if title:
                batch.append({
                    "source":    "online",
                    "source_id": m.group(1),
                    "title":     title,
                    "lyrics":    "",
                })

        songs.extend(batch)
        if not batch:
            break
        page += 10

    return songs


async def scrape_online_lyrics(client: httpx.AsyncClient, song_id: str) -> str:
    """Fetch full lyrics text from song_lyrics.php?song_id=ID."""
    url  = f"{ONLINE_BASE}/song_lyrics.php?song_id={song_id}"
    html = await fetch_html(client, url, delay=DELAYS["online_lyrics"])
    if not html:
        return ""

    soup    = BeautifulSoup(html, "html.parser")
    # Lyrics in <span class="versedisplay2">
    spans   = soup.find_all("span", class_="versedisplay2")
    stanzas = []
    for span in spans:
        # Convert <br> to newline
        for br in span.find_all("br"):
            br.replace_with("\n")
        text = span.get_text().strip()
        if text:
            stanzas.append(text)

    return "\n\n".join(stanzas)


# ─────────────────────────────────────────────────────────────────────────────
# ══ SOURCE 2: wikimezmur.org — SITEMAP APPROACH ══
# ─────────────────────────────────────────────────────────────────────────────

async def scrape_wiki_from_sitemap(client: httpx.AsyncClient) -> list[dict]:
    """
    Parse wikimezmur.org/sitemap.xml to build a clean hierarchy.

    URL patterns in sitemap:
      /am/Artist                → artist page    (2 path segments after /)
      /am/Artist/Album          → album page     (3 segments)
      /am/Artist/Album/Track    → track page     (4 segments)

    Returns list of artist dicts with "albums" containing track lists.
    """
    print("[wikimezmur] Fetching sitemap.xml…")
    xml_text = await fetch_xml(client, f"{WIKI_BASE}/sitemap.xml", delay=DELAYS["wiki_sitemap"])
    if not xml_text:
        print("  ✗ Could not fetch sitemap — falling back to API")
        return await scrape_wiki_from_api(client)

    # Parse XML
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError as e:
        print(f"  ✗ Sitemap XML parse error: {e}")
        return await scrape_wiki_from_api(client)

    # Namespace handling
    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    locs = [el.text or "" for el in root.findall(".//sm:loc", ns)]
    if not locs:
        # Try without namespace
        locs = [el.text or "" for el in root.findall(".//loc")]
    if not locs:
        print("  ✗ No URLs found in sitemap")
        return await scrape_wiki_from_api(client)

    print(f"  {len(locs)} URLs in sitemap")

    # Classify URLs by depth
    artists:  dict[str, dict] = {}   # key = artist_slug
    albums:   dict[tuple, list] = {} # key = (artist_slug, album_slug)

    for loc in locs:
        loc = loc.strip()
        if not loc:
            continue

        # Strip domain → path
        path = loc.replace("http://www.wikimezmur.org", "") \
                  .replace("https://www.wikimezmur.org", "") \
                  .replace("http://wikimezmur.org", "") \
                  .replace("https://wikimezmur.org", "")

        # Must start with /am/
        if not path.startswith("/am/"):
            continue

        # Split into segments, decode percent-encoding
        try:
            from urllib.parse import unquote
            segments = [unquote(s) for s in path[4:].split("/") if s]
        except Exception:
            continue

        # Skip non-person pages
        if not segments:
            continue

        # Depth 1: /am/Artist
        if len(segments) == 1:
            slug = segments[0]
            if is_likely_person_name(slug.replace("_", " ")):
                if slug not in artists:
                    artists[slug] = {
                        "name":      slug.replace("_", " "),
                        "wiki_path": slug,
                        "albums":    [],
                        "songs":     [],
                    }

        # Depth 2: /am/Artist/Album
        elif len(segments) == 2:
            a_slug, alb_slug = segments
            if a_slug not in artists and is_likely_person_name(a_slug.replace("_", " ")):
                artists[a_slug] = {
                    "name":      a_slug.replace("_", " "),
                    "wiki_path": a_slug,
                    "albums":    [],
                    "songs":     [],
                }
            if a_slug in artists:
                key = (a_slug, alb_slug)
                if key not in albums:
                    albums[key] = []

        # Depth 3: /am/Artist/Album/Track
        elif len(segments) == 3:
            a_slug, alb_slug, track_slug = segments
            if a_slug not in artists and is_likely_person_name(a_slug.replace("_", " ")):
                artists[a_slug] = {
                    "name":      a_slug.replace("_", " "),
                    "wiki_path": a_slug,
                    "albums":    [],
                    "songs":     [],
                }
            if a_slug in artists:
                key = (a_slug, alb_slug)
                if key not in albums:
                    albums[key] = []
                track_url = f"/am/{a_slug}/{alb_slug}/{track_slug}"
                albums[key].append({
                    "source":    "wiki",
                    "source_id": track_url,
                    "title":     track_slug.replace("_", " "),
                    "lyrics":    "",
                })

    # Assemble albums into artists
    seen_album_keys = set()
    for (a_slug, alb_slug), tracks in albums.items():
        if a_slug not in artists:
            continue
        album = {
            "title":     alb_slug.replace("_", " "),
            "wiki_slug": alb_slug,
            "tracks":    tracks,
        }
        artists[a_slug]["albums"].append(album)
        seen_album_keys.add((a_slug, alb_slug))

    result = list(artists.values())
    total_tracks = sum(len(t) for a in result for alb in a["albums"] for t in [alb["tracks"]])
    print(f"  ✓ {len(result)} artists, {sum(len(a['albums']) for a in result)} albums, "
          f"{total_tracks} tracks from sitemap")
    return result


async def scrape_wiki_from_api(client: httpx.AsyncClient) -> list[dict]:
    """Fallback: use MediaWiki API with much stricter filtering than v1."""
    print("[wikimezmur] Fallback: MediaWiki API…")
    artists = []
    cont = None
    while True:
        url = f"{WIKI_BASE}/api.php?action=query&list=allpages&aplimit=500&format=json&apnamespace=0"
        if cont:
            from urllib.parse import quote
            url += f"&apcontinue={quote(cont)}"
        try:
            r = await client.get(url, timeout=20)
            data = r.json()
        except Exception:
            break

        for p in data.get("query", {}).get("allpages", []):
            title = p.get("title", "")
            if is_likely_person_name(title):
                path = title.replace(" ", "_")
                artists.append({"name": title, "wiki_path": path, "albums": [], "songs": []})

        cont = data.get("continue", {}).get("apcontinue")
        if not cont:
            break
        await asyncio.sleep(0.3)

    print(f"  ✓ {len(artists)} artists from API (after strict filter)")
    return artists


async def scrape_wiki_lyrics(client: httpx.AsyncClient, wiki_path: str) -> str:
    """Fetch lyrics from a wiki track page."""
    url  = f"{WIKI_BASE}{wiki_path}"
    html = await fetch_html(client, url, delay=DELAYS["wiki_lyrics"])
    if not html:
        return ""

    soup = BeautifulSoup(html, "html.parser")

    # Get the main content div
    content = (
        soup.find("div", id="mw-content-text")
        or soup.find("div", class_="mw-parser-output")
        or soup
    )

    # Extract paragraphs and pre blocks
    stanzas = []
    for tag in content.find_all(["p", "pre"]):
        for br in tag.find_all("br"):
            br.replace_with("\n")
        text = tag.get_text().strip()
        # Filter out navigation text (short lines, links-only paragraphs)
        if text and len(text) > 10:
            stanzas.append(text)

    # Remove duplicate stanzas
    seen = set()
    unique = []
    for s in stanzas:
        key = " ".join(s.lower().split())[:50]
        if key not in seen:
            seen.add(key)
            unique.append(s)

    return "\n\n".join(unique)


# ─────────────────────────────────────────────────────────────────────────────
# Deduplication
# ─────────────────────────────────────────────────────────────────────────────

def merge_artists(online_list: list[dict], wiki_list: list[dict]) -> list[dict]:
    """
    Merge artists from both sources. Dedup by normalised name.
    Artists on both sources get source='both'.
    """
    by_key: dict[str, dict] = {}

    for a in online_list:
        key = normalise_name(a["name"])
        by_key[key] = {
            "name":           a["name"],
            "name_normalized": key,
            "source":         "online",
            "online_encoded": a.get("online_encoded"),
            "wiki_path":      None,
            "song_count":     a.get("song_count", 0),
            "albums":         [],
            "songs":          a.get("songs", []),
        }

    for a in wiki_list:
        key = normalise_name(a["name"])
        if key in by_key:
            # Merge: this artist exists in both sources
            by_key[key]["source"]    = "both"
            by_key[key]["wiki_path"] = a["wiki_path"]
            by_key[key]["albums"]    = a.get("albums", [])
            # Add wiki songs not already present
            existing_ids = {s["source_id"] for s in by_key[key]["songs"]}
            for alb in a.get("albums", []):
                for t in alb.get("tracks", []):
                    if t["source_id"] not in existing_ids:
                        existing_ids.add(t["source_id"])
                        by_key[key]["songs"].append(t)
        else:
            by_key[key] = {
                "name":           a["name"],
                "name_normalized": key,
                "source":         "wiki",
                "online_encoded": None,
                "wiki_path":      a["wiki_path"],
                "song_count":     sum(len(alb.get("tracks", [])) for alb in a.get("albums", [])),
                "albums":         a.get("albums", []),
                "songs":          [],
            }

    merged = sorted(by_key.values(), key=lambda x: x["name"].lower())
    print(f"  Merged → {len(merged)} unique artists "
          f"({sum(1 for a in merged if a['source'] == 'both')} on both sources)")
    return merged


# ─────────────────────────────────────────────────────────────────────────────
# Main orchestrator
# ─────────────────────────────────────────────────────────────────────────────

async def main(args: argparse.Namespace):
    data         = load_progress()
    done_keys    = {normalise_name(a["name"]) for a in data["artists"]}

    limits = httpx.Limits(max_keepalive_connections=5, max_connections=10)
    async with httpx.AsyncClient(limits=limits, timeout=30) as client:

        # ── Step 1 & 2: Gather artist lists ───────────────────────────────────
        online_raw: list[dict] = []
        wiki_raw:   list[dict] = []

        if not args.wiki_only:
            print("\n══ Step 1: onlinemezmur.com artist list ══")
            online_raw = await scrape_online_artists(client)

        if not args.online_only:
            print("\n══ Step 2: wikimezmur.org artist list ══")
            wiki_raw = await scrape_wiki_from_sitemap(client)

        # ── Step 3: Merge & deduplicate ────────────────────────────────────────
        print("\n══ Step 3: Merge + deduplicate ══")
        merged = merge_artists(online_raw, wiki_raw)

        if args.limit:
            merged = merged[: args.limit]
            print(f"  (limited to {args.limit} artists for this run)")

        # ── Step 4: Per-artist songs + lyrics ─────────────────────────────────
        print(f"\n══ Step 4: Songs & lyrics for {len(merged)} artists ══")

        for i, artist in enumerate(merged):
            key = artist["name_normalized"]

            if key in done_keys:
                print(f"  [{i+1}/{len(merged)}] SKIP: {artist['name']}")
                continue

            print(f"  [{i+1}/{len(merged)}] {artist['name']} ({artist['source']})")

            # Online songs
            if artist["source"] in ("online", "both") and artist["online_encoded"]:
                songs = await scrape_online_songs(client, artist["online_encoded"])
                artist["songs"].extend(songs)
                artist["song_count"] = len(artist["songs"]) + sum(
                    len(a.get("tracks", [])) for a in artist["albums"])
                print(f"    → {len(songs)} songs from onlinemezmur")

            # Lyrics for online songs
            if not args.artists_only and artist["source"] in ("online", "both"):
                fetched = 0
                for song in artist["songs"]:
                    if song.get("lyrics") or song["source"] != "online":
                        continue
                    lyrics = await scrape_online_lyrics(client, song["source_id"])
                    song["lyrics"] = lyrics
                    if lyrics:
                        fetched += 1
                if fetched:
                    print(f"    → {fetched} online lyrics fetched")

            # Wiki lyrics (for album tracks)
            if not args.artists_only and artist["source"] in ("wiki", "both"):
                wiki_fetched = 0
                for album in artist.get("albums", []):
                    for track in album.get("tracks", []):
                        if track.get("lyrics"):
                            continue
                        lyrics = await scrape_wiki_lyrics(client, track["source_id"])
                        track["lyrics"] = lyrics
                        if lyrics:
                            wiki_fetched += 1
                if wiki_fetched:
                    print(f"    → {wiki_fetched} wiki lyrics fetched")

            data["artists"].append(artist)
            done_keys.add(key)

            # Save every 100 artists
            if len(data["artists"]) % 100 == 0:
                save_progress(data)
                total_songs = sum(len(a["songs"]) + sum(len(alb.get("tracks", [])) for alb in a["albums"])
                                  for a in data["artists"])
                print(f"\n  💾 Checkpoint: {len(data['artists'])} artists, {total_songs} songs")

    # Final save
    save_progress(data)

    total_artists = len(data["artists"])
    total_albums  = sum(len(a["albums"]) for a in data["artists"])
    total_songs   = sum(
        len(a["songs"]) + sum(len(alb.get("tracks", [])) for alb in a["albums"])
        for a in data["artists"]
    )
    total_lyrics  = sum(
        sum(1 for s in a["songs"] if s.get("lyrics"))
        + sum(1 for alb in a["albums"] for t in alb.get("tracks", []) if t.get("lyrics"))
        for a in data["artists"]
    )

    print(f"""
╔══════════════════════════════════════╗
║  Scraping complete!                  ║
║  Artists  : {total_artists:<6}               ║
║  Albums   : {total_albums:<6}               ║
║  Songs    : {total_songs:<6}               ║
║  w/lyrics : {total_lyrics:<6}               ║
║  Saved    : {str(DATA_FILE)[:30]}  ║
╚══════════════════════════════════════╝

Next step:
  railway run python -m scripts.seed_mezmur
""")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Scrape mezmur catalogue")
    p.add_argument("--limit",        type=int, default=None,  help="Max artists to process")
    p.add_argument("--artists-only", action="store_true",     help="Skip lyrics fetch")
    p.add_argument("--wiki-only",    action="store_true",     help="Only scrape wikimezmur.org")
    p.add_argument("--online-only",  action="store_true",     help="Only scrape onlinemezmur.com")
    args = p.parse_args()
    asyncio.run(main(args))
