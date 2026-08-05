"""
Seed the mezmur tables from data/mezmur_data.json.

Run AFTER scrape_mezmur.py has produced the JSON file locally.

Usage:
    python -m scripts.seed_mezmur [--force] [--validate-only] [--data-file path]
    python -m scripts.seed_mezmur --progress-interval 1

    --force          Drop and re-seed all mezmur data.
    --validate-only  Read and validate the JSON file without touching the DB.
    --data-file      Seed/validate a specific JSON artifact.
    --insert-mode    bulk (default) or orm.
    --progress-interval
                     Seconds between progress updates. Use 0 for every event.

On Railway:
    railway run python -m scripts.seed_mezmur
"""
import sys
import json
import gzip
import asyncio
import argparse
import hashlib
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import text, select, func, insert
from app.database import AsyncSessionLocal, engine, Base
from app.models import MezmurArtist, MezmurAlbum, MezmurSong
from scripts.mezmur_crawler.utils import clean_ethiopic_text, infer_mezmur_language

DATA_FILE = Path(__file__).parent.parent / "data" / "mezmur_data.json"
ONLINE_BASE = "https://onlinemezmur.com"
WIKI_BASE = "https://wikimezmur.org"

SKIP_ARTIST_NAMES = {
    "bible",
    "choirs",
    "collections",
    "contact",
    "copyright",
    "church directory",
    "privacy",
    "terms",
    "sitemap",
}

IMPORT_META_DDL = """
CREATE TABLE IF NOT EXISTS mezmur_import_meta (
    id INTEGER PRIMARY KEY,
    data_sha256 VARCHAR(64) NOT NULL,
    artist_count INTEGER NOT NULL DEFAULT 0,
    album_count INTEGER NOT NULL DEFAULT 0,
    song_count INTEGER NOT NULL DEFAULT 0,
    lyrics_count INTEGER NOT NULL DEFAULT 0,
    imported_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

MEZMUR_SCHEMA_UPGRADE_SQL = """
ALTER TABLE mezmur_artists ADD COLUMN IF NOT EXISTS name_am VARCHAR(300);
ALTER TABLE mezmur_albums ADD COLUMN IF NOT EXISTS title_am VARCHAR(400);
ALTER TABLE mezmur_songs ADD COLUMN IF NOT EXISTS title_am VARCHAR(400);
ALTER TABLE mezmur_songs ADD COLUMN IF NOT EXISTS language VARCHAR(10) NOT NULL DEFAULT 'am';
CREATE INDEX IF NOT EXISTS ix_mezmur_song_language ON mezmur_songs (language);
"""


@dataclass(frozen=True)
class DataSummary:
    artist_count: int
    album_count: int
    song_count: int
    unique_song_count: int
    lyrics_count: int
    duplicate_song_keys: int


class SeedProgress:
    def __init__(
        self,
        *,
        total_artists: int,
        total_albums: int,
        total_songs: int,
        total_lyrics: int,
        interval_seconds: float,
    ):
        self.total_artists = total_artists
        self.total_albums = total_albums
        self.total_songs = total_songs
        self.total_lyrics = total_lyrics
        self.interval_seconds = max(interval_seconds, 0.0)
        self.started_at = time.perf_counter()
        self.last_printed_at = 0.0
        self.artists = 0
        self.albums = 0
        self.songs = 0
        self.lyrics = 0
        self.duplicates = 0

    def artist_done(self):
        self.artists += 1
        self.print_if_due()

    def album_done(self):
        self.albums += 1
        self.print_if_due()

    def song_done(self, *, has_lyrics: bool):
        self.songs += 1
        self.lyrics += int(has_lyrics)
        self.print_if_due()

    def duplicate_done(self):
        self.duplicates += 1
        self.print_if_due()

    def print_if_due(self, *, force: bool = False):
        now = time.perf_counter()
        if not force and self.interval_seconds and now - self.last_printed_at < self.interval_seconds:
            return
        if not force and self.interval_seconds == 0 and self.artists == 0 and self.songs == 0:
            return

        elapsed = max(now - self.started_at, 0.001)
        song_rate = self.songs / elapsed
        remaining_songs = max(self.total_songs - self.songs, 0)
        eta_seconds = remaining_songs / song_rate if song_rate > 0 else None
        percent = (self.songs / self.total_songs * 100) if self.total_songs else 100.0

        print(
            "[seed progress] "
            f"{percent:6.2f}% | "
            f"artists {self.artists}/{self.total_artists} | "
            f"albums {self.albums}/{self.total_albums} | "
            f"songs {self.songs}/{self.total_songs} | "
            f"lyrics {self.lyrics}/{self.total_lyrics} | "
            f"dupes {self.duplicates} | "
            f"{song_rate:,.1f} songs/s | "
            f"elapsed {_format_duration(elapsed)} | "
            f"eta {_format_duration(eta_seconds) if eta_seconds is not None else 'calculating'}",
            flush=True,
        )
        self.last_printed_at = now


async def main(
    force: bool,
    validate_only: bool,
    data_file: Path = DATA_FILE,
    progress_interval: float = 2.0,
    insert_mode: str = "bulk",
):
    data_file = data_file.resolve()
    data, data_hash = _load_data(data_file)
    raw_artist_count = len(data.get("artists", []))
    data = _clean_data(data)
    errors = _validate_data(data)
    source_summary = _summarize_data(data)

    print("Mezmur data file:", flush=True)
    print(f"   Path:    {data_file}", flush=True)
    print(f"   SHA256:  {data_hash}", flush=True)
    print(f"   Artists: {source_summary.artist_count}", flush=True)
    print(f"   Albums:  {source_summary.album_count}", flush=True)
    print(
        f"   Songs:   {source_summary.song_count} ({source_summary.unique_song_count} unique)",
        flush=True,
    )
    print(f"   Lyrics:  {source_summary.lyrics_count}", flush=True)
    if raw_artist_count != source_summary.artist_count:
        print(
            f"   Cleaned: skipped {raw_artist_count - source_summary.artist_count} empty/meta artists",
            flush=True,
        )

    if source_summary.duplicate_song_keys:
        print(
            f"   Note:    {source_summary.duplicate_song_keys} duplicate song source keys will be skipped",
            flush=True,
        )

    if errors:
        print("\nInvalid mezmur data:")
        for error in errors[:25]:
            print(f"   - {error}")
        if len(errors) > 25:
            print(f"   - ...and {len(errors) - 25} more errors")
        sys.exit(1)

    if validate_only:
        print("\nValidation passed. DB was not changed.", flush=True)
        return

    artists_data = data.get("artists", [])

    # Create tables if they don't exist.
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text(IMPORT_META_DDL))
        for statement in MEZMUR_SCHEMA_UPGRADE_SQL.strip().split(";"):
            if statement.strip():
                await conn.execute(text(statement))

    async with AsyncSessionLocal() as db:
        existing = (await db.execute(select(func.count()).select_from(MezmurArtist))).scalar()
        existing_hash = await _get_import_hash(db)

        if existing > 0 and not force:
            if existing_hash is None:
                print(
                    "\nMezmur rows already exist, but they were created before import hash tracking.",
                    flush=True,
                )
                print(
                    "Skipping to avoid overwriting production data. Run with --force after the intended full JSON is committed.",
                    flush=True,
                )
                await engine.dispose()
                return
            if existing_hash == data_hash:
                print(f"\nMezmur data already matches this JSON ({existing} artists).", flush=True)
                await engine.dispose()
                return
            print("\nMezmur JSON changed since the last import. Re-importing from committed data file.", flush=True)

        if existing > 0:
            reason = "--force" if force else "changed JSON hash"
            print(f"\n{reason}: replacing {existing} existing artist records...", flush=True)
            await db.execute(text("DELETE FROM mezmur_songs"))
            await db.execute(text("DELETE FROM mezmur_albums"))
            await db.execute(text("DELETE FROM mezmur_artists"))

        total_artists = len(artists_data)
        total_songs = 0
        total_lyrics = 0
        total_albums = 0
        skipped_duplicates = 0
        seen_song_keys: set[tuple[str, str]] = set()

        progress = SeedProgress(
            total_artists=total_artists,
            total_albums=source_summary.album_count,
            total_songs=source_summary.unique_song_count,
            total_lyrics=source_summary.lyrics_count,
            interval_seconds=progress_interval,
        )

        print(f"\nSeeding {total_artists} artists...", flush=True)
        print(f"Insert mode: {insert_mode}", flush=True)
        progress.print_if_due(force=True)

        if insert_mode == "bulk":
            total_artists, total_albums, total_songs, total_lyrics, skipped_duplicates = (
                await _bulk_insert_mezmur(
                    db=db,
                    artists_data=artists_data,
                    progress=progress,
                )
            )

            await _write_import_meta(
                db=db,
                data_hash=data_hash,
                artist_count=total_artists,
                album_count=total_albums,
                song_count=total_songs,
                lyrics_count=total_lyrics,
            )

            await db.commit()
            progress.print_if_due(force=True)
            print("\nDone.", flush=True)
            print(f"   Artists: {total_artists}", flush=True)
            print(f"   Albums:  {total_albums}", flush=True)
            print(f"   Songs:   {total_songs}", flush=True)
            print(f"   Lyrics:  {total_lyrics}", flush=True)
            if skipped_duplicates:
                print(f"   Skipped duplicate song keys: {skipped_duplicates}", flush=True)
            await engine.dispose()
            return

        for i, a_data in enumerate(artists_data):
            artist = MezmurArtist(
                name=a_data["name"],
                name_am=a_data.get("name_am"),
                name_normalized = a_data.get("name_normalized", a_data["name"].lower()),
                source=a_data.get("source", "online"),
                online_encoded=a_data.get("online_encoded"),
                wiki_path=a_data.get("wiki_path"),
                song_count=0,
            )
            db.add(artist)
            await db.flush()

            album_id_by_slug: dict[str, int] = {}
            artist_song_count = 0

            for alb_data in a_data.get("albums", []):
                album = MezmurAlbum(
                    artist_id=artist.id,
                    title=alb_data["title"],
                    title_am=alb_data.get("title_am"),
                    wiki_slug=alb_data.get("wiki_slug"),
                    track_count = len(alb_data.get("tracks", [])),
                )
                db.add(album)
                await db.flush()
                if alb_data.get("wiki_slug"):
                    album_id_by_slug[alb_data["wiki_slug"]] = album.id
                total_albums += 1
                progress.album_done()

                # Insert tracks (songs) for this album
                for track in alb_data.get("tracks", []):
                    added, has_lyrics = _queue_song(
                        db=db,
                        seen_song_keys=seen_song_keys,
                        artist_id=artist.id,
                        album_id=album.id,
                        song_data=track,
                        default_source="wiki",
                    )
                    if added:
                        artist_song_count += 1
                        total_songs += 1
                        total_lyrics += int(has_lyrics)
                        progress.song_done(has_lyrics=has_lyrics)
                    else:
                        skipped_duplicates += 1
                        progress.duplicate_done()

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

                added, has_lyrics = _queue_song(
                    db=db,
                    seen_song_keys=seen_song_keys,
                    artist_id=artist.id,
                    album_id=album_id,
                    song_data=s_data,
                    default_source="online",
                )
                if added:
                    artist_song_count += 1
                    total_songs += 1
                    total_lyrics += int(has_lyrics)
                    progress.song_done(has_lyrics=has_lyrics)
                else:
                    skipped_duplicates += 1
                    progress.duplicate_done()

            artist.song_count = artist_song_count
            progress.artist_done()

            if (i + 1) % 100 == 0:
                await db.flush()
                progress.print_if_due(force=True)

        await _write_import_meta(
            db=db,
            data_hash=data_hash,
            artist_count=total_artists,
            album_count=total_albums,
            song_count=total_songs,
            lyrics_count=total_lyrics,
        )

        await db.commit()
        progress.print_if_due(force=True)
        print("\nDone.", flush=True)
        print(f"   Artists: {total_artists}", flush=True)
        print(f"   Albums:  {total_albums}", flush=True)
        print(f"   Songs:   {total_songs}", flush=True)
        print(f"   Lyrics:  {total_lyrics}", flush=True)
        if skipped_duplicates:
            print(f"   Skipped duplicate song keys: {skipped_duplicates}", flush=True)
        await engine.dispose()


async def _bulk_insert_mezmur(
    *,
    db,
    artists_data: list[dict],
    progress: SeedProgress,
    song_batch_size: int = 1000,
) -> tuple[int, int, int, int, int]:
    artist_rows = [
        {
            "name": a_data["name"],
            "name_am": a_data.get("name_am"),
            "name_normalized": a_data.get("name_normalized", a_data["name"].lower()),
            "source": a_data.get("source", "online"),
            "online_encoded": a_data.get("online_encoded"),
            "wiki_path": a_data.get("wiki_path"),
            "song_count": 0,
        }
        for a_data in artists_data
    ]

    if artist_rows:
        await db.execute(insert(MezmurArtist), artist_rows)
        for _row in artist_rows:
            progress.artist_done()
        progress.print_if_due(force=True)

    artist_norms = [row["name_normalized"] for row in artist_rows]
    artist_id_by_norm: dict[str, int] = {}
    if artist_norms:
        result = await db.execute(
            select(MezmurArtist.id, MezmurArtist.name_normalized)
            .where(MezmurArtist.name_normalized.in_(artist_norms))
        )
        artist_id_by_norm = {name_normalized: artist_id for artist_id, name_normalized in result.all()}

    missing_artists = sorted(set(artist_norms) - set(artist_id_by_norm))
    if missing_artists:
        raise RuntimeError(f"Bulk insert failed to resolve {len(missing_artists)} artist ids")

    album_rows: list[dict] = []
    album_artist_ids: set[int] = set()
    for a_data in artists_data:
        artist_id = artist_id_by_norm[a_data.get("name_normalized", a_data["name"].lower())]
        for alb_data in a_data.get("albums", []):
            album_rows.append(
                {
                    "artist_id": artist_id,
                    "title": alb_data["title"],
                    "title_am": alb_data.get("title_am"),
                    "wiki_slug": alb_data.get("wiki_slug"),
                    "track_count": len(alb_data.get("tracks", [])),
                }
            )
            album_artist_ids.add(artist_id)

    if album_rows:
        await db.execute(insert(MezmurAlbum), album_rows)
        for _row in album_rows:
            progress.album_done()
        progress.print_if_due(force=True)

    album_id_by_key: dict[tuple[int, str], int] = {}
    if album_artist_ids:
        result = await db.execute(
            select(MezmurAlbum.id, MezmurAlbum.artist_id, MezmurAlbum.wiki_slug)
            .where(MezmurAlbum.artist_id.in_(album_artist_ids))
        )
        album_id_by_key = {
            (artist_id, wiki_slug): album_id
            for album_id, artist_id, wiki_slug in result.all()
            if wiki_slug
        }

    song_rows: list[dict] = []
    total_lyrics = 0
    skipped_duplicates = 0
    seen_song_keys: set[tuple[str, str]] = set()

    for a_data in artists_data:
        artist_id = artist_id_by_norm[a_data.get("name_normalized", a_data["name"].lower())]

        for alb_data in a_data.get("albums", []):
            wiki_slug = alb_data.get("wiki_slug")
            album_id = album_id_by_key.get((artist_id, wiki_slug)) if wiki_slug else None
            for track in alb_data.get("tracks", []):
                row = _bulk_song_row(
                    seen_song_keys=seen_song_keys,
                    artist_id=artist_id,
                    album_id=album_id,
                    song_data=track,
                    default_source="wiki",
                )
                if row is None:
                    skipped_duplicates += 1
                    continue
                total_lyrics += int(row["has_lyrics"])
                song_rows.append(row)

        for s_data in a_data.get("songs", []):
            album_id = None
            if s_data.get("source") == "wiki":
                parts = s_data["source_id"].strip("/").split("/")
                if len(parts) >= 4:
                    album_id = album_id_by_key.get((artist_id, parts[2]))

            row = _bulk_song_row(
                seen_song_keys=seen_song_keys,
                artist_id=artist_id,
                album_id=album_id,
                song_data=s_data,
                default_source="online",
            )
            if row is None:
                skipped_duplicates += 1
                continue
            total_lyrics += int(row["has_lyrics"])
            song_rows.append(row)

    for start in range(0, len(song_rows), song_batch_size):
        batch = song_rows[start : start + song_batch_size]
        await db.execute(insert(MezmurSong), batch)
        for row in batch:
            progress.song_done(has_lyrics=row["has_lyrics"])
        progress.print_if_due(force=True)

    await db.execute(text("""
        UPDATE mezmur_artists
        SET song_count = (
            SELECT COUNT(*)
            FROM mezmur_songs
            WHERE mezmur_songs.artist_id = mezmur_artists.id
        )
    """))

    return (
        len(artist_rows),
        len(album_rows),
        len(song_rows),
        total_lyrics,
        skipped_duplicates,
    )


def _bulk_song_row(
    *,
    seen_song_keys: set[tuple[str, str]],
    artist_id: int,
    album_id: int | None,
    song_data: dict,
    default_source: str,
) -> dict | None:
    source = song_data.get("source", default_source)
    source_id = str(song_data["source_id"])
    key = (source, source_id)
    if key in seen_song_keys:
        return None
    seen_song_keys.add(key)

    lyrics_json, arrangement = _structure_song_data(song_data)
    return {
        "artist_id": artist_id,
        "album_id": album_id,
        "title": song_data["title"],
        "title_am": song_data.get("title_am"),
        "language": song_data.get("language") or "am",
        "source": source,
        "source_id": source_id,
        "lyrics_json": lyrics_json,
        "arrangement": arrangement,
        "has_lyrics": bool(lyrics_json),
    }


def _format_duration(seconds: float) -> str:
    seconds = max(int(seconds), 0)
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {secs:02d}s"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def _load_data(data_file: Path = DATA_FILE) -> tuple[dict, str]:
    gz_file = data_file.with_suffix(data_file.suffix + ".gz")

    if data_file.exists():
        raw = data_file.read_bytes()
    elif gz_file.exists():
        # The expanded catalogue is ~52 MB, too large to track in git, so the
        # gzip is committed instead and expanded here. Hashing the expanded
        # bytes keeps data_hash identical for both paths.
        with gzip.open(gz_file, "rb") as fh:
            raw = fh.read()
    else:
        print(f"Data file not found: {data_file}")
        print(f"Expected {data_file.name} or {gz_file.name}")
        print("Run: python -m scripts.scrape_mezmur first")
        sys.exit(1)

    data_hash = hashlib.sha256(raw).hexdigest()

    try:
        data = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        print(f"Invalid JSON in {data_file}: {exc}")
        sys.exit(1)

    return data, data_hash


def _validate_data(data: dict) -> list[str]:
    errors: list[str] = []
    artists_data = data.get("artists", [])

    if not isinstance(artists_data, list) or not artists_data:
        return ["No artist data in JSON. Run the scraper first."]

    for artist_idx, artist in enumerate(artists_data):
        artist_ref = f"artists[{artist_idx}]"
        if not isinstance(artist, dict):
            errors.append(f"{artist_ref} must be an object")
            continue
        if not artist.get("name"):
            errors.append(f"{artist_ref}.name is required")

        for album_idx, album in enumerate(artist.get("albums", [])):
            album_ref = f"{artist_ref}.albums[{album_idx}]"
            if not album.get("title"):
                errors.append(f"{album_ref}.title is required")
            for track_idx, track in enumerate(album.get("tracks", [])):
                _validate_song(errors, track, f"{album_ref}.tracks[{track_idx}]")

        for song_idx, song in enumerate(artist.get("songs", [])):
            _validate_song(errors, song, f"{artist_ref}.songs[{song_idx}]")

    return errors


def _validate_song(errors: list[str], song: dict, ref: str):
    if not isinstance(song, dict):
        errors.append(f"{ref} must be an object")
        return
    if not song.get("title"):
        errors.append(f"{ref}.title is required")
    if not song.get("source_id"):
        errors.append(f"{ref}.source_id is required")


def _clean_data(data: dict) -> dict:
    cleaned_artists = []
    for artist in data.get("artists", []):
        cleaned = _clean_artist(artist)
        if cleaned:
            cleaned_artists.append(cleaned)
    return {**data, "artists": cleaned_artists}


def _clean_artist(artist: dict) -> dict | None:
    if not isinstance(artist, dict):
        return None

    name = _clean_inline(artist.get("name", ""))
    if not name or name.lower() in SKIP_ARTIST_NAMES:
        return None

    songs = [_clean_song(song, "online") for song in artist.get("songs", [])]
    songs = [song for song in songs if song]

    albums = []
    for album in artist.get("albums", []):
        title = _clean_inline(album.get("title", ""))
        tracks = [_clean_song(track, "wiki") for track in album.get("tracks", [])]
        tracks = [track for track in tracks if track]
        if title and tracks:
            albums.append({
                **album,
                "title": title,
                "title_am": clean_ethiopic_text(album.get("title_am") or title),
                "wiki_slug": album.get("wiki_slug"),
                "tracks": tracks,
            })

    if not songs and not any(album.get("tracks") for album in albums):
        return None

    return {
        **artist,
        "name": name,
        "name_am": clean_ethiopic_text(artist.get("name_am") or name),
        "name_normalized": _clean_inline(artist.get("name_normalized", "")) or name.lower(),
        "source": artist.get("source", "online"),
        "online_encoded": artist.get("online_encoded"),
        "wiki_path": artist.get("wiki_path"),
        "song_count": len(songs) + sum(len(album.get("tracks", [])) for album in albums),
        "songs": songs,
        "albums": albums,
    }


def _clean_song(song: dict, default_source: str) -> dict | None:
    if not isinstance(song, dict):
        return None
    title = _clean_inline(song.get("title", ""))
    source_id = _clean_inline(str(song.get("source_id", "")))
    if not title or not source_id:
        return None
    sections = _clean_source_sections(song.get("sections", []))
    lyrics = _clean_lyrics(song.get("lyrics", "")) or _lyrics_from_sections(sections)
    return {
        **song,
        "source": song.get("source", default_source),
        "source_id": source_id,
        "title": title,
        "title_am": clean_ethiopic_text(song.get("title_am") or title),
        "language": song.get("language") or infer_mezmur_language(title=title, lyrics=lyrics),
        "lyrics": lyrics,
        "sections": sections,
    }


def _summarize_data(data: dict) -> DataSummary:
    artists_data = data.get("artists", [])
    album_count = 0
    song_count = 0
    lyrics_count = 0
    duplicate_song_keys = 0
    seen_song_keys: set[tuple[str, str]] = set()

    for artist in artists_data:
        albums = artist.get("albums", [])
        album_count += len(albums)

        for album in albums:
            for track in album.get("tracks", []):
                song_count += 1
                lyrics_count += int(bool((track.get("lyrics") or "").strip()))
                duplicate_song_keys += int(_is_duplicate_song_key(seen_song_keys, track, "wiki"))

        for song in artist.get("songs", []):
            song_count += 1
            lyrics_count += int(bool((song.get("lyrics") or "").strip()))
            duplicate_song_keys += int(_is_duplicate_song_key(seen_song_keys, song, "online"))

    return DataSummary(
        artist_count=len(artists_data),
        album_count=album_count,
        song_count=song_count,
        unique_song_count=len(seen_song_keys),
        lyrics_count=lyrics_count,
        duplicate_song_keys=duplicate_song_keys,
    )


def _is_duplicate_song_key(
    seen_song_keys: set[tuple[str, str]],
    song_data: dict,
    default_source: str,
) -> bool:
    source_id = song_data.get("source_id")
    if not source_id:
        return False
    key = (song_data.get("source", default_source), str(source_id))
    if key in seen_song_keys:
        return True
    seen_song_keys.add(key)
    return False


def _queue_song(
    db,
    seen_song_keys: set[tuple[str, str]],
    artist_id: int,
    album_id: int | None,
    song_data: dict,
    default_source: str,
) -> tuple[bool, bool]:
    source = song_data.get("source", default_source)
    source_id = str(song_data["source_id"])
    key = (source, source_id)
    if key in seen_song_keys:
        return False, False

    seen_song_keys.add(key)
    lyrics_json, arrangement = _structure_song_data(song_data)
    has_lyrics = bool(lyrics_json)

    db.add(MezmurSong(
        artist_id=artist_id,
        album_id=album_id,
        title=song_data["title"],
        title_am=song_data.get("title_am"),
        language=song_data.get("language") or "am",
        source=source,
        source_id=source_id,
        lyrics_json=lyrics_json,
        arrangement=arrangement,
        has_lyrics=has_lyrics,
    ))

    return True, has_lyrics


def _structure_song_data(song_data: dict) -> tuple[str | None, str | None]:
    sections = _structure_source_sections(song_data.get("sections", []))
    if sections:
        arrangement = ",".join(
            section["key"]
            for section in sections
            if section.get("key")
        )
        api_sections = [
            {
                "type": section["type"],
                "label": section["label"],
                "sort_order": section["sort_order"],
                "lyrics_am": section["lyrics_am"],
                "lyrics_en": section.get("lyrics_en", ""),
            }
            for section in sections
        ]
        return json.dumps(api_sections, ensure_ascii=False), arrangement or None

    return _structure_lyrics(song_data.get("lyrics", ""))


async def _get_import_hash(db) -> str | None:
    result = await db.execute(text("SELECT data_sha256 FROM mezmur_import_meta WHERE id = 1"))
    return result.scalar_one_or_none()


async def _write_import_meta(
    db,
    data_hash: str,
    artist_count: int,
    album_count: int,
    song_count: int,
    lyrics_count: int,
):
    await db.execute(text("DELETE FROM mezmur_import_meta WHERE id = 1"))
    await db.execute(
        text("""
            INSERT INTO mezmur_import_meta (
                id, data_sha256, artist_count, album_count, song_count, lyrics_count
            ) VALUES (
                1, :data_hash, :artist_count, :album_count, :song_count, :lyrics_count
            )
        """),
        {
            "data_hash": data_hash,
            "artist_count": artist_count,
            "album_count": album_count,
            "song_count": song_count,
            "lyrics_count": lyrics_count,
        },
    )


def _clean_source_sections(value) -> list[dict]:
    if not isinstance(value, list):
        return []

    sections = []
    for i, section in enumerate(value):
        if not isinstance(section, dict):
            continue
        lyrics = _clean_lyrics(section.get("lyrics_am") or section.get("text") or "")
        if not lyrics:
            continue
        section_type = _clean_inline(section.get("type", "")) or "verse"
        if section_type not in {"verse", "chorus", "bridge", "intro", "outro"}:
            section_type = "verse"
        key = _clean_inline(section.get("key", ""))
        if not key:
            key = "c" if section_type == "chorus" else f"v{i + 1}"
        sections.append({
            "type": section_type,
            "label": _clean_inline(section.get("label", "")) or ("አዝ" if section_type == "chorus" else f"Verse {i + 1}"),
            "key": key,
            "sort_order": _safe_sort_order(section.get("sort_order"), i),
            "lyrics_am": lyrics,
            "lyrics_en": _clean_lyrics(section.get("lyrics_en", "")),
        })
    return sections


def _structure_source_sections(value) -> list[dict]:
    return _clean_source_sections(value)


def _lyrics_from_sections(sections: list[dict]) -> str:
    return "\n\n".join(
        section["lyrics_am"]
        for section in sections
        if section.get("lyrics_am")
    )


def _safe_sort_order(value, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _structure_lyrics(raw: str) -> tuple[str | None, str | None]:
    """
    Convert raw lyrics text into structured JSON sections.
    Splits on double-newlines (paragraphs) and assigns verse/chorus labels.
    Returns (lyrics_json, arrangement) or (None, None) if raw is empty.
    """
    raw = _clean_lyrics(raw)
    if not raw:
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


def _clean_inline(value: str) -> str:
    return " ".join((value or "").replace("\r", " ").replace("\t", " ").split())


def _clean_lyrics(value: str) -> str:
    if not value:
        return ""

    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    text = "\n".join(line.strip() for line in text.split("\n"))

    paragraphs = []
    current = []
    for line in text.split("\n"):
        if line:
            current.append(line)
        elif current:
            paragraphs.append("\n".join(current))
            current = []
    if current:
        paragraphs.append("\n".join(current))

    return "\n\n".join(paragraphs)


def _to_ethiopic_numeral(n: int) -> str:
    ethiopic = ["፩", "፪", "፫", "፬", "፭", "፮", "፯", "፰", "፱", "፲"]
    if 1 <= n <= 10:
        return ethiopic[n - 1]
    return str(n)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="Re-seed (drop + insert)")
    parser.add_argument("--validate-only", action="store_true", help="Validate JSON without changing the DB")
    parser.add_argument("--data-file", type=Path, default=DATA_FILE, help="Mezmur JSON file to seed")
    parser.add_argument(
        "--progress-interval",
        type=float,
        default=2.0,
        help="Seconds between live progress updates. Use 0 to print every artist/song event.",
    )
    parser.add_argument(
        "--insert-mode",
        choices=("bulk", "orm"),
        default="bulk",
        help="Insert strategy. bulk is much faster over Railway's public database proxy.",
    )
    args = parser.parse_args()
    asyncio.run(
        main(
            args.force,
            args.validate_only,
            args.data_file,
            args.progress_interval,
            args.insert_mode,
        )
    )
