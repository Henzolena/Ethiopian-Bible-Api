"""
Mezmur (Ethiopian Christian Songs) endpoints.

Prefix: /api/v1/mezmur

Endpoints:
  GET /mezmur/artists              — list all artists (paginated, searchable)
  GET /mezmur/artists/{id}         — artist detail
  GET /mezmur/artists/{id}/albums  — artist's albums
  GET /mezmur/artists/{id}/songs   — flat song list (all sources)
  GET /mezmur/albums/{id}          — album detail with track list
  GET /mezmur/songs/{id}           — song detail + structured lyrics
  GET /mezmur/search               — full-text search across title / artist / lyrics
  GET /mezmur/random               — random song with lyrics
  GET /mezmur/stats                — catalogue statistics
"""
import json
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, or_, and_
from app.database import get_db
from app.models import MezmurArtist, MezmurAlbum, MezmurSong

router = APIRouter(prefix="/mezmur", tags=["Mezmur"])
ONLINE_BASE = "https://onlinemezmur.com"
WIKI_BASE = "https://wikimezmur.org"
MEZMUROCH_BASE = "https://www.mezmuroch.com"
SOURCE_FILTERS = {"mezmuroch", "online", "wiki", "both", "multiple"}
LANGUAGE_FILTERS = {"am", "en"}


# ─────────────────────────────────────────────────────────────────────────────
# Artists
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/artists")
async def list_artists(
    q:      Optional[str] = Query(None, description="Search by artist name"),
    source: Optional[str] = Query(None, description="Filter: mezmuroch | online | wiki | both | multiple"),
    language: Optional[str] = Query(None, description="Filter artists with songs in: am | en"),
    page:   int           = Query(1,  ge=1),
    limit:  int           = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    """List all artists with optional search, source, and language filters."""
    stmt = select(MezmurArtist)
    if q:
        q_norm = q.strip().lower()
        term = f"%{q.strip()}%"
        stmt = stmt.where(or_(
            MezmurArtist.name_normalized.contains(q_norm),
            MezmurArtist.name.ilike(term),
            MezmurArtist.name_am.ilike(term),
        ))
    if source:
        stmt = stmt.where(_artist_source_filter(source))
    if language:
        language_norm = _normalize_language_filter(language)
        artists_with_language = (
            select(MezmurSong.artist_id)
            .where(MezmurSong.language == language_norm)
            .distinct()
        )
        stmt = stmt.where(MezmurArtist.id.in_(artists_with_language))
    stmt = stmt.order_by(func.coalesce(MezmurArtist.name_am, MezmurArtist.name)).offset((page - 1) * limit).limit(limit)
    result = await db.execute(stmt)
    artists = result.scalars().all()
    language_counts = await _artist_language_counts(db, [a.id for a in artists])

    # total count
    count_stmt = select(func.count()).select_from(MezmurArtist)
    if q:
        q_norm = q.strip().lower()
        term = f"%{q.strip()}%"
        count_stmt = count_stmt.where(or_(
            MezmurArtist.name_normalized.contains(q_norm),
            MezmurArtist.name.ilike(term),
            MezmurArtist.name_am.ilike(term),
        ))
    if source:
        count_stmt = count_stmt.where(_artist_source_filter(source))
    if language:
        language_norm = _normalize_language_filter(language)
        artists_with_language = (
            select(MezmurSong.artist_id)
            .where(MezmurSong.language == language_norm)
            .distinct()
        )
        count_stmt = count_stmt.where(MezmurArtist.id.in_(artists_with_language))
    total = (await db.execute(count_stmt)).scalar() or 0

    return {
        "total": total,
        "page": page,
        "page_size": limit,
        "artists": [_artist_out(a, language_counts.get(a.id)) for a in artists],
    }


@router.get("/artists/{artist_id}")
async def get_artist(artist_id: int, db: AsyncSession = Depends(get_db)):
    """Artist detail."""
    artist = await _get_or_404(db, MezmurArtist, artist_id)
    language_counts = await _artist_language_counts(db, [artist_id])
    return _artist_out(artist, language_counts.get(artist_id))


@router.get("/artists/{artist_id}/albums")
async def get_artist_albums(artist_id: int, db: AsyncSession = Depends(get_db)):
    """Albums for an artist."""
    await _get_or_404(db, MezmurArtist, artist_id)
    stmt = select(MezmurAlbum).where(MezmurAlbum.artist_id == artist_id).order_by(MezmurAlbum.title)
    albums = (await db.execute(stmt)).scalars().all()
    return {"artist_id": artist_id, "albums": [_album_out(a) for a in albums]}


@router.get("/artists/{artist_id}/songs")
async def get_artist_songs(
    artist_id: int,
    page:  int = Query(1,  ge=1),
    limit: int = Query(50, ge=1, le=200),
    with_lyrics: bool = Query(False),
    language: Optional[str] = Query(None, description="Filter: am | en"),
    db: AsyncSession = Depends(get_db),
):
    """Flat list of all songs by an artist."""
    await _get_or_404(db, MezmurArtist, artist_id)
    stmt = select(MezmurSong).where(MezmurSong.artist_id == artist_id)
    if with_lyrics:
        stmt = stmt.where(MezmurSong.has_lyrics == True)
    if language:
        stmt = stmt.where(_song_language_filter(language))
    stmt = stmt.order_by(MezmurSong.title).offset((page - 1) * limit).limit(limit)
    songs = (await db.execute(stmt)).scalars().all()
    return {
        "artist_id": artist_id,
        "page": page,
        "page_size": limit,
        "songs": [_song_out(s) for s in songs],
    }


# ─────────────────────────────────────────────────────────────────────────────
# Albums
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/albums/{album_id}")
async def get_album(album_id: int, db: AsyncSession = Depends(get_db)):
    """Album detail with track list."""
    album = await _get_or_404(db, MezmurAlbum, album_id)
    stmt  = select(MezmurSong).where(MezmurSong.album_id == album_id).order_by(MezmurSong.id)
    tracks = (await db.execute(stmt)).scalars().all()
    out = _album_out(album)
    out["tracks"] = [_song_out(t) for t in tracks]
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Songs / Lyrics
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/songs/{song_id}")
async def get_song(song_id: int, db: AsyncSession = Depends(get_db)):
    """Song detail including structured lyrics sections."""
    song = await _get_or_404(db, MezmurSong, song_id)
    artist_row = (await db.execute(
        select(MezmurArtist.name, MezmurArtist.name_am).where(MezmurArtist.id == song.artist_id)
    )).first()
    artist_name, artist_name_am = artist_row if artist_row else ("", "")
    return _song_out(
        song,
        include_lyrics=True,
        artist_name=artist_name,
        artist_name_am=artist_name_am,
    )


@router.get("/random")
async def random_song(
    with_lyrics: bool = Query(True),
    language: Optional[str] = Query(None, description="Filter: am | en"),
    db: AsyncSession = Depends(get_db),
):
    """Return a random song (with lyrics if available)."""
    stmt = select(MezmurSong)
    if with_lyrics:
        stmt = stmt.where(MezmurSong.has_lyrics == True)
    if language:
        stmt = stmt.where(_song_language_filter(language))
    stmt = stmt.order_by(func.random()).limit(1)
    song = (await db.execute(stmt)).scalars().first()
    if not song:
        raise HTTPException(status_code=404, detail="No songs available")
    return _song_out(song, include_lyrics=True)


# ─────────────────────────────────────────────────────────────────────────────
# Search
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/search")
async def search_mezmur(
    q:     str = Query(..., min_length=2, description="Search term (title / artist / lyric text)"),
    page:  int = Query(1,  ge=1),
    limit: int = Query(20, ge=1, le=100),
    language: Optional[str] = Query(None, description="Filter: am | en"),
    db: AsyncSession = Depends(get_db),
):
    """Full-text search across song title and lyrics."""
    term = f"%{q.strip()}%"

    # Search in song titles
    songs_stmt = (
        select(MezmurSong, MezmurArtist.name, MezmurArtist.name_am)
        .join(MezmurArtist, MezmurSong.artist_id == MezmurArtist.id)
        .where(or_(
            MezmurSong.title.ilike(term),
            MezmurSong.title_am.ilike(term),
            MezmurArtist.name.ilike(term),
            MezmurArtist.name_am.ilike(term),
            MezmurSong.lyrics_json.ilike(term),
        ))
        .order_by(func.coalesce(MezmurSong.title_am, MezmurSong.title))
        .offset((page - 1) * limit)
        .limit(limit)
    )
    if language:
        songs_stmt = songs_stmt.where(_song_language_filter(language))
    rows = (await db.execute(songs_stmt)).all()

    count_stmt = (
        select(func.count())
        .select_from(MezmurSong)
        .join(MezmurArtist, MezmurSong.artist_id == MezmurArtist.id)
        .where(or_(
            MezmurSong.title.ilike(term),
            MezmurSong.title_am.ilike(term),
            MezmurArtist.name.ilike(term),
            MezmurArtist.name_am.ilike(term),
            MezmurSong.lyrics_json.ilike(term),
        ))
    )
    if language:
        count_stmt = count_stmt.where(_song_language_filter(language))
    total = (await db.execute(count_stmt)).scalar() or 0

    return {
        "q":        q,
        "total":    total,
        "page":     page,
        "page_size": limit,
        "results":  [
            _song_out(song, include_lyrics=False, artist_name=artist_name, artist_name_am=artist_name_am)
            for song, artist_name, artist_name_am in rows
        ],
    }


# ─────────────────────────────────────────────────────────────────────────────
# Statistics
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/stats")
async def mezmur_stats(db: AsyncSession = Depends(get_db)):
    """Catalogue statistics."""
    artists  = (await db.execute(select(func.count()).select_from(MezmurArtist))).scalar()
    albums   = (await db.execute(select(func.count()).select_from(MezmurAlbum))).scalar()
    songs    = (await db.execute(select(func.count()).select_from(MezmurSong))).scalar()
    with_lyr = (await db.execute(
        select(func.count()).select_from(MezmurSong).where(MezmurSong.has_lyrics == True)
    )).scalar()
    amharic_songs = (await db.execute(
        select(func.count()).select_from(MezmurSong).where(MezmurSong.language == "am")
    )).scalar()
    english_songs = (await db.execute(
        select(func.count()).select_from(MezmurSong).where(MezmurSong.language == "en")
    )).scalar()
    return {
        "artists":       artists,
        "albums":        albums,
        "songs":         songs,
        "amharic_songs": amharic_songs,
        "english_songs": english_songs,
        "songs_with_lyrics": with_lyr,
        "lyrics_coverage": f"{round(with_lyr / songs * 100, 1)}%" if songs else "0%",
    }


# ─────────────────────────────────────────────────────────────────────────────
# Output helpers
# ─────────────────────────────────────────────────────────────────────────────

async def _artist_language_counts(db: AsyncSession, artist_ids: list[int]) -> dict[int, dict[str, int]]:
    if not artist_ids:
        return {}

    rows = (await db.execute(
        select(MezmurSong.artist_id, MezmurSong.language, func.count())
        .where(MezmurSong.artist_id.in_(artist_ids))
        .group_by(MezmurSong.artist_id, MezmurSong.language)
    )).all()

    counts = {
        artist_id: {"amharic_song_count": 0, "english_song_count": 0}
        for artist_id in artist_ids
    }
    for artist_id, language, count in rows:
        key = "english_song_count" if language == "en" else "amharic_song_count"
        counts.setdefault(artist_id, {"amharic_song_count": 0, "english_song_count": 0})[key] += count or 0
    return counts


def _artist_out(a: MezmurArtist, language_counts: Optional[dict[str, int]] = None) -> dict:
    language_counts = language_counts or {}
    return {
        "id":         a.id,
        "name":       a.name,
        "name_am":    a.name_am or "",
        "display_name": a.name_am or a.name,
        "source":     a.source,
        "song_count": a.song_count,
        "amharic_song_count": language_counts.get("amharic_song_count", 0),
        "english_song_count": language_counts.get("english_song_count", 0),
        "has_mezmuroch": a.source in ("mezmuroch", "multiple"),
        "has_online": a.source in ("online", "both") or bool(a.online_encoded),
        "has_wiki":   a.source in ("wiki", "both") or bool(a.wiki_path),
    }


def _album_out(a: MezmurAlbum) -> dict:
    return {
        "id":          a.id,
        "artist_id":   a.artist_id,
        "title":       a.title,
        "title_am":    a.title_am or "",
        "display_title": a.title_am or a.title,
        "track_count": a.track_count,
    }


def _song_out(
    s: MezmurSong,
    include_lyrics: bool = True,
    artist_name: Optional[str] = None,
    artist_name_am: Optional[str] = None,
) -> dict:
    out = {
        "id":        s.id,
        "title":     s.title,
        "title_am":  s.title_am or "",
        "display_title": s.title_am or s.title,
        "language":  s.language or "am",
        "artist_id": s.artist_id,
        "album_id":  s.album_id,
        "source":    s.source,
        "source_id":  s.source_id,
        "source_url": _source_url(s),
        "has_lyrics": s.has_lyrics,
    }
    if artist_name is not None:
        out["artist"] = artist_name
        out["artist_am"] = artist_name_am or ""
        out["display_artist"] = artist_name_am or artist_name
    if include_lyrics and s.lyrics_json:
        try:
            out["sections"]    = json.loads(s.lyrics_json)
            out["arrangement"] = s.arrangement.split(",") if s.arrangement else []
        except Exception:
            out["sections"]    = []
            out["arrangement"] = []
    return out


async def _get_or_404(db: AsyncSession, model, pk: int):
    obj = (await db.execute(select(model).where(model.id == pk))).scalars().first()
    if not obj:
        raise HTTPException(status_code=404, detail=f"{model.__name__} {pk} not found")
    return obj


def _source_url(song: MezmurSong) -> str | None:
    if song.source == "mezmuroch":
        return f"{MEZMUROCH_BASE}{song.source_id}"
    if song.source == "online":
        return f"{ONLINE_BASE}/song_lyrics.php?song_id={song.source_id}"
    if song.source == "wiki":
        return f"{WIKI_BASE}{song.source_id}"
    return None


def _artist_source_filter(source: str):
    if source not in SOURCE_FILTERS:
        raise HTTPException(status_code=400, detail="Invalid source filter")
    if source == "both":
        return MezmurArtist.source == "both"
    if source == "multiple":
        return MezmurArtist.source == "multiple"
    if source in ("online", "wiki"):
        return or_(
            MezmurArtist.source == source,
            MezmurArtist.source == "both",
            MezmurArtist.source == "multiple",
        )
    return or_(
        MezmurArtist.source == source,
        MezmurArtist.source == "multiple",
    )


def _song_language_filter(language: str):
    normalized = _normalize_language_filter(language)
    return MezmurSong.language == normalized


def _normalize_language_filter(language: str) -> str:
    normalized = language.strip().lower()
    if normalized not in LANGUAGE_FILTERS:
        raise HTTPException(status_code=400, detail="Invalid language filter")
    return normalized
