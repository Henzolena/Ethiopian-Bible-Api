"""
Mezmur (Ethiopian Christian Songs) endpoints.

Prefix: /api/v1/mezmur

Endpoints:
  GET /mezmur/artists              — list all artists (paginated, searchable)
  GET /mezmur/artists/{id}         — artist detail
  GET /mezmur/artists/{id}/albums  — artist's albums (wiki source)
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


# ─────────────────────────────────────────────────────────────────────────────
# Artists
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/artists")
async def list_artists(
    q:      Optional[str] = Query(None, description="Search by artist name"),
    source: Optional[str] = Query(None, description="Filter: online | wiki | both"),
    page:   int           = Query(1,  ge=1),
    limit:  int           = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    """List all artists with optional search and source filter."""
    stmt = select(MezmurArtist)
    if q:
        q_norm = q.strip().lower()
        stmt = stmt.where(MezmurArtist.name_normalized.contains(q_norm))
    if source:
        if source == "both":
            stmt = stmt.where(MezmurArtist.source == "both")
        elif source in ("online", "wiki"):
            stmt = stmt.where(or_(
                MezmurArtist.source == source,
                MezmurArtist.source == "both"
            ))
    stmt = stmt.order_by(MezmurArtist.name).offset((page - 1) * limit).limit(limit)
    result = await db.execute(stmt)
    artists = result.scalars().all()

    # total count
    count_stmt = select(func.count()).select_from(MezmurArtist)
    if q:
        count_stmt = count_stmt.where(MezmurArtist.name_normalized.contains(q.strip().lower()))
    if source:
        if source == "both":
            count_stmt = count_stmt.where(MezmurArtist.source == "both")
        elif source in ("online", "wiki"):
            count_stmt = count_stmt.where(or_(MezmurArtist.source == source, MezmurArtist.source == "both"))
    total = (await db.execute(count_stmt)).scalar() or 0

    return {
        "total": total,
        "page": page,
        "page_size": limit,
        "artists": [_artist_out(a) for a in artists],
    }


@router.get("/artists/{artist_id}")
async def get_artist(artist_id: int, db: AsyncSession = Depends(get_db)):
    """Artist detail."""
    artist = await _get_or_404(db, MezmurArtist, artist_id)
    return _artist_out(artist)


@router.get("/artists/{artist_id}/albums")
async def get_artist_albums(artist_id: int, db: AsyncSession = Depends(get_db)):
    """Albums for an artist (populated from wikimezmur.org)."""
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
    db: AsyncSession = Depends(get_db),
):
    """Flat list of all songs by an artist."""
    await _get_or_404(db, MezmurArtist, artist_id)
    stmt = select(MezmurSong).where(MezmurSong.artist_id == artist_id)
    if with_lyrics:
        stmt = stmt.where(MezmurSong.has_lyrics == True)
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
    return _song_out(song, include_lyrics=True)


@router.get("/random")
async def random_song(
    with_lyrics: bool = Query(True),
    db: AsyncSession = Depends(get_db),
):
    """Return a random song (with lyrics if available)."""
    stmt = select(MezmurSong)
    if with_lyrics:
        stmt = stmt.where(MezmurSong.has_lyrics == True)
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
    db: AsyncSession = Depends(get_db),
):
    """Full-text search across song title and lyrics."""
    term = f"%{q.strip()}%"

    # Search in song titles
    songs_stmt = (
        select(MezmurSong)
        .join(MezmurArtist, MezmurSong.artist_id == MezmurArtist.id)
        .where(or_(
            MezmurSong.title.ilike(term),
            MezmurArtist.name.ilike(term),
            MezmurSong.lyrics_json.ilike(term),
        ))
        .order_by(MezmurSong.title)
        .offset((page - 1) * limit)
        .limit(limit)
    )
    songs = (await db.execute(songs_stmt)).scalars().all()

    count_stmt = (
        select(func.count())
        .select_from(MezmurSong)
        .join(MezmurArtist, MezmurSong.artist_id == MezmurArtist.id)
        .where(or_(
            MezmurSong.title.ilike(term),
            MezmurArtist.name.ilike(term),
            MezmurSong.lyrics_json.ilike(term),
        ))
    )
    total = (await db.execute(count_stmt)).scalar() or 0

    return {
        "q":        q,
        "total":    total,
        "page":     page,
        "page_size": limit,
        "results":  [_song_out(s, include_lyrics=False) for s in songs],
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
    return {
        "artists":       artists,
        "albums":        albums,
        "songs":         songs,
        "songs_with_lyrics": with_lyr,
        "lyrics_coverage": f"{round(with_lyr / songs * 100, 1)}%" if songs else "0%",
    }


# ─────────────────────────────────────────────────────────────────────────────
# Output helpers
# ─────────────────────────────────────────────────────────────────────────────

def _artist_out(a: MezmurArtist) -> dict:
    return {
        "id":         a.id,
        "name":       a.name,
        "source":     a.source,
        "song_count": a.song_count,
        "has_online": a.source in ("online", "both"),
        "has_wiki":   a.source in ("wiki",   "both"),
    }


def _album_out(a: MezmurAlbum) -> dict:
    return {
        "id":          a.id,
        "artist_id":   a.artist_id,
        "title":       a.title,
        "track_count": a.track_count,
    }


def _song_out(s: MezmurSong, include_lyrics: bool = True) -> dict:
    out = {
        "id":        s.id,
        "title":     s.title,
        "artist_id": s.artist_id,
        "album_id":  s.album_id,
        "source":    s.source,
        "has_lyrics": s.has_lyrics,
    }
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
