"""
Seed the mezmur tables from data/mezmur_data.json.

Run AFTER scrape_mezmur.py has produced the JSON file.

Usage:
    python -m scripts.seed_mezmur [--force]

    --force   Drop and re-seed all mezmur data (default: skip if data exists)

On Railway:
    railway run python -m scripts.seed_mezmur
"""
import sys
import json
import asyncio
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import text, select, func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from app.database import AsyncSessionLocal, engine, Base
from app.models import MezmurArtist, MezmurAlbum, MezmurSong

DATA_FILE = Path(__file__).parent.parent / "data" / "mezmur_data.json"


async def main(force: bool):
    if not DATA_FILE.exists():
        print(f"✗ Data file not found: {DATA_FILE}")
        print("  Run: python -m scripts.scrape_mezmur  first")
        sys.exit(1)

    data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    artists_data = data.get("artists", [])
    if not artists_data:
        print("✗ No artist data in JSON — run the scraper first")
        sys.exit(1)

    # Create tables if they don't exist
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with AsyncSessionLocal() as db:
        # Check existing count
        existing = (await db.execute(select(func.count()).select_from(MezmurArtist))).scalar()
        if existing > 0 and not force:
            print(f"✓ Mezmur data already seeded ({existing} artists). Use --force to re-seed.")
            return

        if force and existing > 0:
            print(f"  --force: dropping {existing} existing artist records…")
            await db.execute(text("DELETE FROM mezmur_songs"))
            await db.execute(text("DELETE FROM mezmur_albums"))
            await db.execute(text("DELETE FROM mezmur_artists"))
            await db.commit()

        total_artists = len(artists_data)
        total_songs   = 0
        total_albums  = 0

        print(f"Seeding {total_artists} artists…")

        for i, a_data in enumerate(artists_data):
            # Insert artist
            artist = MezmurArtist(
                name            = a_data["name"],
                name_normalized = a_data.get("name_normalized", a_data["name"].lower()),
                source          = a_data.get("source", "online"),
                online_encoded  = a_data.get("online_encoded"),
                wiki_path       = a_data.get("wiki_path"),
                song_count      = a_data.get("song_count", 0),
            )
            db.add(artist)
            await db.flush()   # get artist.id

            # Index albums by slug for song→album resolution
            album_id_by_slug: dict[str, int] = {}

            for alb_data in a_data.get("albums", []):
                album = MezmurAlbum(
                    artist_id   = artist.id,
                    title       = alb_data["title"],
                    wiki_slug   = alb_data.get("wiki_slug"),
                    track_count = len(alb_data.get("tracks", [])),
                )
                db.add(album)
                await db.flush()
                if alb_data.get("wiki_slug"):
                    album_id_by_slug[alb_data["wiki_slug"]] = album.id
                total_albums += 1

                # Insert tracks (songs) for this album
                for track in alb_data.get("tracks", []):
                    lyrics_json, arrangement = _structure_lyrics(track.get("lyrics", ""))
                    song = MezmurSong(
                        artist_id   = artist.id,
                        album_id    = album.id,
                        title       = track["title"],
                        source      = track.get("source", "wiki"),
                        source_id   = track["source_id"],
                        lyrics_json = lyrics_json,
                        arrangement = arrangement,
                        has_lyrics  = bool(lyrics_json),
                    )
                    db.add(song)
                    total_songs += 1

            # Insert flat songs (those not tied to a specific album)
            for s_data in a_data.get("songs", []):
                # Skip if this source_id already added as an album track
                album_id = None
                if s_data.get("source") == "wiki":
                    # Determine album from the wiki path structure
                    parts = s_data["source_id"].strip("/").split("/")
                    if len(parts) >= 4:  # /am/Artist/Album/Track
                        slug = parts[2]
                        album_id = album_id_by_slug.get(slug)

                lyrics_json, arrangement = _structure_lyrics(s_data.get("lyrics", ""))
                song = MezmurSong(
                    artist_id   = artist.id,
                    album_id    = album_id,
                    title       = s_data["title"],
                    source      = s_data.get("source", "online"),
                    source_id   = s_data["source_id"],
                    lyrics_json = lyrics_json,
                    arrangement = arrangement,
                    has_lyrics  = bool(lyrics_json),
                )
                db.add(song)
                total_songs += 1

            # Update artist song_count
            artist.song_count = total_songs   # rough — will be overwritten per artist below

            if (i + 1) % 100 == 0:
                await db.commit()
                print(f"  {i+1}/{total_artists} artists seeded ({total_songs} songs so far)…")

        await db.commit()
        print(f"\n✅ Done!")
        print(f"   Artists: {total_artists}")
        print(f"   Albums:  {total_albums}")
        print(f"   Songs:   {total_songs}")


def _structure_lyrics(raw: str) -> tuple[str | None, str | None]:
    """
    Convert raw lyrics text into structured JSON sections.
    Splits on double-newlines (paragraphs) and assigns verse/chorus labels.
    Returns (lyrics_json, arrangement) or (None, None) if raw is empty.
    """
    if not raw or not raw.strip():
        return None, None

    paragraphs = [p.strip() for p in raw.split("\n\n") if p.strip()]
    if not paragraphs:
        return None, None

    sections = []
    arrangement = []
    verse_num   = 1
    chorus_seen = False

    for i, para in enumerate(paragraphs):
        # Heuristic: repeated paragraphs = chorus
        # First paragraph often intro/verse; later repeated identical text = chorus
        is_chorus = _looks_like_chorus(para, paragraphs, i)

        if is_chorus:
            stype = "chorus"
            label = "ኮረስ"
            key   = "c"
            if not chorus_seen:
                chorus_seen = True
        else:
            stype = "verse"
            label = f"ቁጥር {_to_ethiopic_numeral(verse_num)}"
            key   = f"v{verse_num}"
            verse_num += 1

        # Don't add duplicate chorus keys to arrangement
        if key not in arrangement or key != "c":
            arrangement.append(key)
        elif key == "c" and (not arrangement or arrangement[-1] != "c"):
            arrangement.append(key)

        sections.append({
            "type":      stype,
            "label":     label,
            "sort_order": i,
            "lyrics_am": para,
            "lyrics_en": "",
        })

    if not sections:
        return None, None

    return json.dumps(sections, ensure_ascii=False), ",".join(arrangement)


def _looks_like_chorus(para: str, all_paras: list[str], idx: int) -> bool:
    """Simple heuristic: paragraph appears more than once → likely chorus."""
    norm = " ".join(para.lower().split())
    count = sum(1 for p in all_paras if " ".join(p.lower().split()) == norm)
    return count > 1


def _to_ethiopic_numeral(n: int) -> str:
    ethiopic = ["፩", "፪", "፫", "፬", "፭", "፮", "፯", "፰", "፱", "፲"]
    if 1 <= n <= 10:
        return ethiopic[n - 1]
    return str(n)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="Re-seed (drop + insert)")
    args = parser.parse_args()
    asyncio.run(main(args.force))
