"""
Verse of the Day — Audio Generation Router
===========================================

POST /api/v1/votd/generate-audio   (admin, X-Admin-Key required)
GET  /api/v1/votd/today             (public)
POST /api/v1/votd/request-audio     (public — iOS on-demand fallback)

Full pipeline
-------------
  1. Fetch all previously used verse refs from Supabase (deduplication list).
  2. Mistral selects today's verse — must be spiritually complete, standalone,
     growth-oriented, and not a repeat of any previous VOTD.
  3. Fetch the selected verse text (NIV) from the Bible API.
  4. Mistral writes a 180-200 word spoken devotional (read → illuminate →
     apply → bless), warm NIV-level language, specific to THIS verse.
  5. Gemini 2.5 Flash Preview TTS converts the script to audio.
  6. ffmpeg converts WAV/PCM → MP3 @ 128 kbps.
  7. MP3 uploaded to Supabase Storage "verse-audio/{date}.mp3".
  8. verse_of_the_day row updated: real verse info + audio_url + status="ready".

Required Railway env vars
--------------------------
  SUPABASE_URL          https://bpqauxqpibaosnbvhito.supabase.co
  SUPABASE_ANON_KEY     anon key  (public reads)
  SUPABASE_SERVICE_KEY  service_role key (DB writes + Storage)
  VOTD_ADMIN_KEY        secret for the admin endpoint
  GEMINI_API_KEY        already set ✅
  MISTRAL_API_KEY       already set ✅
"""

from __future__ import annotations

import io
import json
from datetime import date

import httpx
from fastapi import APIRouter, BackgroundTasks, Header, HTTPException

from app.config import settings

router = APIRouter(prefix="/votd", tags=["Verse of the Day"])

AUDIO_BUCKET     = "verse-audio"
GEMINI_TTS_MODEL = "gemini-2.5-flash-preview-tts"

# Four voices rotated daily — warm/expressive variety for devotional content
# Date-based selection: same day always gets same voice; changes each new day
GEMINI_VOICES = ["Zephyr", "Aoede", "Fenrir", "Charon"]

# ── Curated verse pool ────────────────────────────────────────────────────────
# 60 hand-picked NIV verses that each stand completely alone, contain a timeless
# spiritual truth, and are meaningful for morning devotion. Covers a wide range
# of Scripture: Psalms, Proverbs, Gospels, Epistles, Prophets, and more.
# Adding to this list over time keeps the daily rotation fresh for years.

_VERSE_POOL = [
    # Psalms
    ("PSA",  23,  1), ("PSA",  46, 10), ("PSA",  27,  1), ("PSA",  34,  8),
    ("PSA",  91,  1), ("PSA", 119,105), ("PSA", 121,  2), ("PSA", 143,  8),
    ("PSA",  37,  4), ("PSA",   1,  1),
    # Proverbs
    ("PRO",   3,  5), ("PRO",  31, 25), ("PRO",   4, 23), ("PRO",  16,  3),
    ("PRO",   3,  6),
    # Gospel of John
    ("JHN",   3, 16), ("JHN",  14,  6), ("JHN",  10, 10), ("JHN",   8, 32),
    ("JHN",  15,  5),
    # Gospel of Matthew
    ("MAT",   6, 33), ("MAT",  11, 28), ("MAT",   5,  6), ("MAT",   5, 16),
    # Romans
    ("ROM",   8, 28), ("ROM",   8,  1), ("ROM",  12,  2), ("ROM",  15, 13),
    ("ROM",   5,  8),
    # Epistles
    ("PHP",   4, 13), ("PHP",   4,  6), ("EPH",   2,  8), ("EPH",   3, 20),
    ("GAL",   2, 20), ("COL",   3, 23), ("HEB",  11,  1), ("HEB",  13,  8),
    ("1CO",  13,  4), ("2CO",   5, 17), ("2TI",   1,  7), ("JAS",   1,  5),
    ("1PE",   5,  7), ("1JN",   4, 19),
    # Isaiah
    ("ISA",  40, 31), ("ISA",  41, 10), ("ISA",  43,  2), ("ISA",  26,  3),
    # Other prophets & OT
    ("JER",  29, 11), ("MIC",   6,  8), ("ZEP",   3, 17), ("LAM",   3, 22),
    ("JOS",   1,  9), ("DEU",  31,  6),
    # New Testament misc
    ("LUK",   1, 37), ("ACT",   1,  8), ("REV",   3, 20), ("MAT",  28, 20),
    ("ROM",  10, 17), ("1TH",   5, 18),
]


# ── Auth ──────────────────────────────────────────────────────────────────────

def _check_admin(key: str | None) -> None:
    if not settings.votd_admin_key or key != settings.votd_admin_key:
        raise HTTPException(status_code=403, detail="Invalid or missing X-Admin-Key")


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("/generate-audio")
async def generate_votd_audio(x_admin_key: str | None = Header(None, alias="X-Admin-Key")):
    """
    Admin endpoint — generates today's VOTD audio.
    Idempotent: returns immediately if already ready.
    Call once per day via cron or manually.
    """
    _check_admin(x_admin_key)

    if not settings.supabase_url or not settings.supabase_service_key:
        raise HTTPException(status_code=503, detail=(
            "SUPABASE_URL and SUPABASE_SERVICE_KEY must be set in Railway Variables."
        ))

    today_str = date.today().isoformat()

    async with httpx.AsyncClient(timeout=150) as http:
        existing = await _get_row(http, today_str)
        if existing and existing.get("audio_status") == "ready":
            return {"status": "already_exists", "audio_url": existing["audio_url"], "date": today_str}

        # Mark generating immediately so concurrent cron calls bail early
        await _mark_generating(http, today_str)

        try:
            audio_url, ref, preview = await _full_pipeline(today_str, http)
        except HTTPException:
            await _update_status(http, today_str, None, "failed")
            raise
        except Exception as exc:
            await _update_status(http, today_str, None, "failed")
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    return {"status": "ok", "audio_url": audio_url, "date": today_str, "script_preview": preview}


@router.get("/today")
async def get_today():
    """
    Public endpoint — returns today's verse (from DB, not from /niv/votd)
    plus audio metadata.
    """
    today_str = date.today().isoformat()
    async with httpx.AsyncClient(timeout=15) as http:
        row = await _get_row(http, today_str)
        if not _row_has_verse(row):
            row = await _ensure_today_verse(http, today_str, row)

    verse = _row_to_verse(row)

    return {
        "verse":        verse,
        "audio_url":    row.get("audio_url")    if row else None,
        "audio_status": row.get("audio_status") if row else "pending",
        "date":         today_str,
    }


@router.post("/request-audio")
async def request_votd_audio(background_tasks: BackgroundTasks):
    """
    Public endpoint — iOS calls this on first play-tap when audio isn't ready.
    Returns immediately. iOS polls GET /today every 5 s until status == "ready".
    Concurrent calls are safe: the DB "generating" status prevents duplicate runs.
    """
    if not settings.supabase_url or not settings.supabase_service_key:
        return {"status": "unavailable"}

    today_str = date.today().isoformat()

    async with httpx.AsyncClient(timeout=10) as http:
        row = await _get_row(http, today_str)

        if row and row.get("audio_status") == "ready":
            return {"status": "ready", "audio_url": row.get("audio_url")}

        if row and row.get("audio_status") == "generating":
            return {"status": "generating", "poll_interval_seconds": 5}

        if not _row_has_verse(row):
            row = await _ensure_today_verse(http, today_str, row)
            if not _row_has_verse(row):
                return {"status": "unavailable"}

        # Mark generating synchronously so the next concurrent call sees it
        try:
            await _mark_generating(http, today_str)
        except HTTPException:
            return {"status": "unavailable"}

    background_tasks.add_task(_run_background, today_str)
    return {"status": "generating", "poll_interval_seconds": 5}


# ── Shared pipeline ───────────────────────────────────────────────────────────

async def _full_pipeline(today_str: str, http: httpx.AsyncClient) -> tuple[str, str, str]:
    """
    Runs the complete VOTD pipeline. Returns (audio_url, verse_ref, script_preview).
    The caller is responsible for marking "generating" before calling and
    "failed" on exception.
    """
    existing_row = await _get_row(http, today_str)
    if _row_has_verse(existing_row):
        verse = {
            "book":      existing_row.get("book", ""),
            "book_name": _book_name(existing_row.get("verse_ref", "")),
            "chapter":   existing_row.get("chapter", 0),
            "verse":     existing_row.get("verse", 0),
            "text":      existing_row.get("verse_text", ""),
        }
        print(f"[VOTD] Reusing selected verse {existing_row.get('verse_ref')}")
    else:
        # 1. Fetch all previous verse refs for deduplication
        used_refs = await _get_used_refs(http)
        used_set  = {r.strip().lower() for r in used_refs if r}

        # 2 + 3. Pick from curated pool, skipping already-used verses
        verse = None
        import hashlib
        day_hash = int(hashlib.md5(today_str.encode()).hexdigest(), 16)
        pool     = list(_VERSE_POOL)
        start    = day_hash % len(pool)
        ordered  = pool[start:] + pool[:start]

        for entry in ordered:
            candidate = {"book": entry[0], "chapter": entry[1], "verse": entry[2]}
            v = await _fetch_verse_niv(http, candidate["book"], candidate["chapter"], candidate["verse"])
            if not v:
                continue
            candidate_ref = f"{v['book_name']} {v['chapter']}:{v['verse']}"
            if candidate_ref.strip().lower() in used_set:
                print(f"[VOTD] ⏭  {candidate_ref} already used — trying next in pool")
                continue
            verse = v
            print(f"[VOTD] ✅ Selected {candidate_ref}")
            break

    if not verse:
        raise HTTPException(status_code=502, detail="All curated verses have been used — expand the pool")

    ref  = f"{verse['book_name']} {verse['chapter']}:{verse['verse']}"
    text = verse["text"]

    # 4. Persist the real verse info (overwrites placeholder defaults)
    await _upsert_verse(http, today_str, ref, verse["book"],
                        verse["chapter"], verse["verse"], text)

    # 5. Generate devotional script
    script = await _generate_script(ref, text, http)

    # 6. OpenAI TTS → MP3 bytes (ready to upload, no conversion needed)
    mp3_bytes = await _generate_audio(script, http)

    # 7. Upload to Supabase Storage
    audio_url = await _upload_audio(http, today_str, mp3_bytes)

    # 9. Mark ready
    await _update_status(http, today_str, audio_url, "ready")

    return audio_url, ref, script[:120] + "…"


async def _run_background(today_str: str) -> None:
    """BackgroundTask wrapper for _full_pipeline."""
    async with httpx.AsyncClient(timeout=150) as http:
        try:
            await _full_pipeline(today_str, http)
        except Exception as exc:
            print(f"[VOTD background] failed for {today_str}: {exc}")
            try:
                async with httpx.AsyncClient(timeout=10) as h:
                    await _update_status(h, today_str, None, "failed")
            except Exception:
                pass


# ── Public VOTD verse helpers ─────────────────────────────────────────────────

def _row_has_verse(row: dict | None) -> bool:
    return bool(row and row.get("verse_ref") and row.get("verse_text"))


def _row_to_verse(row: dict | None) -> dict | None:
    if not _row_has_verse(row):
        return None
    return {
        "book":      row.get("book", ""),
        "book_name": _book_name(row.get("verse_ref", "")),
        "chapter":   row.get("chapter", 0),
        "verse":     row.get("verse", 0),
        "text":      row.get("verse_text", ""),
        "language":  "niv",
    }


async def _ensure_today_verse(
    http: httpx.AsyncClient,
    today_str: str,
    row: dict | None,
) -> dict | None:
    """
    Ensures /today can return a verse even before audio generation finishes.
    Audio status is preserved so this does not accidentally mark audio ready or
    generating.
    """
    if _row_has_verse(row):
        return row

    used_refs = await _get_used_refs(http)
    used_set = {r.strip().lower() for r in used_refs if r}

    import hashlib
    day_hash = int(hashlib.md5(today_str.encode()).hexdigest(), 16)
    pool = list(_VERSE_POOL)
    start = day_hash % len(pool)
    ordered = pool[start:] + pool[:start]

    for entry in ordered:
        verse = await _fetch_verse_niv(http, entry[0], entry[1], entry[2])
        if not verse:
            continue

        ref = f"{verse['book_name']} {verse['chapter']}:{verse['verse']}"
        if ref.strip().lower() in used_set:
            continue

        audio_status = (row or {}).get("audio_status") or "pending"
        await _upsert_verse(
            http,
            today_str,
            ref,
            verse["book"],
            verse["chapter"],
            verse["verse"],
            verse["text"],
            audio_status=audio_status,
        )
        return await _get_row(http, today_str)

    return row


# ── AI: verse selection ───────────────────────────────────────────────────────

async def _get_used_refs(http: httpx.AsyncClient) -> list[str]:
    """Returns all previously used VOTD verse references from Supabase."""
    if not settings.supabase_url or not settings.supabase_anon_key:
        return []
    r = await http.get(
        f"{settings.supabase_url}/rest/v1/verse_of_the_day",
        params={"select": "verse_ref", "order": "date.desc"},
        headers=_sb_read_headers(),
    )
    if r.status_code != 200:
        return []
    return [row["verse_ref"] for row in r.json() if row.get("verse_ref")]


def _select_verse_from_pool(used_set: set[str], today_str: str) -> dict | None:
    """
    Selects today's verse from _VERSE_POOL deterministically:
    1. Filter out any verses already used (by matching fetched book_name+ch:v).
    2. Use date-based offset to rotate through the pool so each day is different.
    Returns {"book": "PSA", "chapter": 23, "verse": 1} or None if pool exhausted.
    """
    # Build candidate list — skip already used refs by book+chapter+verse key
    # We can't easily map (book_code, ch, v) → display ref without fetching,
    # so we keep all pool entries and let the post-fetch check in _full_pipeline
    # handle any collision (rare — pool has 60 entries, dedup list grows slowly).
    import hashlib
    # Stable daily offset so the same pool entry isn't always tried first
    day_hash = int(hashlib.md5(today_str.encode()).hexdigest(), 16)
    pool     = list(_VERSE_POOL)
    start    = day_hash % len(pool)
    ordered  = pool[start:] + pool[:start]
    for entry in ordered:
        return {"book": entry[0], "chapter": entry[1], "verse": entry[2]}
    return None


async def _fetch_verse_niv(http: httpx.AsyncClient, book: str, chapter: int, verse: int) -> dict | None:
    """Fetches a single NIV verse from the Bible API."""
    try:
        r = await http.get(
            f"http://localhost:8000/api/v1/niv/books/{book}/{chapter}/{verse}",
            timeout=10,
        )
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None


# ── AI: devotional script ─────────────────────────────────────────────────────

def _strip_markdown(text: str) -> str:
    """
    Remove all markdown formatting from a script before TTS.
    Mistral occasionally adds **bold**, *italic*, # headers, and bullet points
    even when told not to — these get vocalised as noise by OpenAI TTS.
    """
    import re
    text = re.sub(r'\*{1,3}([^*]+)\*{1,3}', r'\1', text)   # **bold** / *italic*
    text = re.sub(r'_{1,2}([^_]+)_{1,2}',   r'\1', text)   # __bold__ / _italic_
    text = re.sub(r'^#{1,6}\s+',             '',   text, flags=re.MULTILINE)  # # headers
    text = re.sub(r'^\s*[-*+]\s+',           '',   text, flags=re.MULTILINE)  # bullet points
    text = re.sub(r'`[^`]+`',               r'',  text)    # `code`
    text = re.sub(r'\n{3,}',               '\n\n', text)   # collapse excess blank lines
    return text.strip()


async def _generate_script(ref: str, text: str, http: httpx.AsyncClient) -> str:
    """Calls Mistral to write a 180-200 word spoken NIV devotional, then strips any markdown."""
    prompt = (
        "You are a warm, Spirit-filled pastor delivering a spoken morning devotional.\n"
        "You are speaking directly into someone's ear — this text will be converted to AUDIO.\n\n"
        f"Today's verse (NIV): {ref} — \"{text}\"\n\n"
        "Deliver a 180-200 word spoken devotional in four flowing paragraphs:\n"
        "Paragraph 1: Read the verse aloud naturally.\n"
        "Paragraph 2: In 2-3 sentences explain what God is saying — simple, heartfelt, not academic.\n"
        "Paragraph 3: In 3-4 sentences bring this truth into today — personal, warm, specific.\n"
        "Paragraph 4: A sincere 1-2 sentence prayer or blessing to close.\n\n"
        "CRITICAL — this is read by a TTS engine:\n"
        "- Output ONLY plain spoken sentences. Zero exceptions.\n"
        "- NO asterisks, NO pound signs, NO dashes used as bullets, NO bold, NO italic.\n"
        "- NO section labels like 'READ:' or 'APPLY:' or 'BLESS:'.\n"
        "- NO markdown of any kind. Plain text only.\n"
        "- Separate paragraphs with a single blank line."
    )

    resp = await http.post(
        f"{settings.mistral_base_url}/chat/completions",
        headers={"Authorization": f"Bearer {settings.mistral_api_key}", "Content-Type": "application/json"},
        json={
            "model":       settings.mistral_model,
            "messages":    [{"role": "user", "content": prompt}],
            "temperature": 0.7,
            "max_tokens":  400,
        },
        timeout=30,
    )

    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Mistral script error {resp.status_code}: {resp.text[:200]}")

    raw = resp.json()["choices"][0]["message"]["content"].strip()
    return _strip_markdown(raw)


# ── AI: Gemini TTS ────────────────────────────────────────────────────────────

async def _generate_audio(script: str, http: httpx.AsyncClient) -> bytes:
    """
    Calls Gemini 2.5 Flash TTS and returns raw WAV/PCM bytes → converted to MP3.
    Rotates through up to 3 API keys on 429 so a single account's daily quota
    never blocks generation.

    Free during preview period; ~$0.25/month after preview ends.
    Fallback keys: set GEMINI_API_KEY_2 / GEMINI_API_KEY_3 in Railway Variables.
    """
    import asyncio, base64

    keys = [k for k in [
        settings.gemini_api_key,
        settings.gemini_api_key_henokrobale,
        settings.gemini_api_key_eeccaustinchurch,
        settings.gemini_api_key_eeccaustinapp,
        settings.gemini_api_key_henzolinasj,
        settings.gemini_api_key_harmonikahn,
        settings.gemini_api_key_robalehenok,
    ] if k]

    if not keys:
        raise HTTPException(status_code=503, detail="No GEMINI_API_KEY configured in Railway Variables")

    # Pick today's voice using day-of-year so each voice gets equal rotation
    # e.g. day 1=Zephyr, day 2=Aoede, day 3=Fenrir, day 4=Charon, day 5=Zephyr…
    today_obj = date.today()
    day_index = (today_obj.timetuple().tm_yday - 1) % len(GEMINI_VOICES)
    voice     = GEMINI_VOICES[day_index]
    print(f"[VOTD] Today's voice: {voice} (day-of-year {today_obj.timetuple().tm_yday}, slot {day_index+1}/{len(GEMINI_VOICES)})")

    url_template = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        "gemini-2.5-flash-preview-tts:generateContent?key={key}"
    )
    payload = {
        "contents": [{"parts": [{"text": script}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice}}},
        },
    }

    for i, key in enumerate(keys):
        label = f"key {i+1}/{len(keys)}"
        resp  = await http.post(url_template.format(key=key), json=payload, timeout=90)

        if resp.status_code == 200:
            part      = resp.json()["candidates"][0]["content"]["parts"][0]["inlineData"]
            mime_type = part.get("mimeType", "audio/wav")
            raw_bytes = base64.b64decode(part["data"])
            print(f"[VOTD] Gemini TTS success with {label} — {len(raw_bytes):,} bytes ({mime_type})")
            return _wav_to_mp3(raw_bytes, mime_type)

        if resp.status_code == 429:
            print(f"[VOTD] Gemini TTS 429 on {label} — {'trying next key' if i+1 < len(keys) else 'all keys exhausted'}")
            if i + 1 < len(keys):
                await asyncio.sleep(2)   # brief pause before next key
                continue

        raise HTTPException(
            status_code=502,
            detail=f"Gemini TTS error {resp.status_code} on {label}: {resp.text[:300]}",
        )

    raise HTTPException(status_code=429, detail="All Gemini API keys quota-exhausted — add more keys or wait until midnight")


def _wav_to_mp3(audio_bytes: bytes, mime_type: str) -> bytes:
    """Converts Gemini WAV/PCM output → MP3 via ffmpeg. Skips if already MP3/MPEG."""
    import os, subprocess, tempfile, base64

    if "mp3" in mime_type.lower() or "mpeg" in mime_type.lower():
        return audio_bytes  # already MP3 — pass straight through

    with tempfile.NamedTemporaryFile(delete=False, suffix=".input") as f:
        f.write(audio_bytes)
        in_path = f.name

    out_path = in_path + ".mp3"
    try:
        cmd = (
            ["ffmpeg", "-y", "-i", in_path, "-b:a", "128k", out_path]
            if "wav" in mime_type.lower()
            else ["ffmpeg", "-y", "-f", "s16le", "-ar", "24000", "-ac", "1",
                  "-i", in_path, "-b:a", "128k", out_path]
        )
        result = subprocess.run(cmd, capture_output=True, timeout=30)
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg: {result.stderr.decode()[-300:]}")
        with open(out_path, "rb") as fmp3:
            return fmp3.read()
    finally:
        os.unlink(in_path)
        if os.path.exists(out_path):
            os.unlink(out_path)


def _to_mp3(audio_bytes: bytes, mime_type: str) -> bytes:
    """Converts audio bytes → MP3 via ffmpeg (Python 3.13-safe, no pydub)."""
    import os, subprocess, tempfile

    if "mp3" in mime_type.lower() or "mpeg" in mime_type.lower():
        return audio_bytes  # OpenAI returns audio/mpeg — already MP3, pass straight through

    with tempfile.NamedTemporaryFile(delete=False, suffix=".input") as f:
        f.write(audio_bytes)
        in_path = f.name

    out_path = in_path + ".mp3"
    try:
        cmd = (
            ["ffmpeg", "-y", "-i", in_path, "-b:a", "128k", out_path]
            if "wav" in mime_type.lower()
            else ["ffmpeg", "-y", "-f", "s16le", "-ar", "24000", "-ac", "1",
                  "-i", in_path, "-b:a", "128k", out_path]
        )
        result = subprocess.run(cmd, capture_output=True, timeout=30)
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg: {result.stderr.decode()[-300:]}")
        with open(out_path, "rb") as fmp3:
            return fmp3.read()
    finally:
        os.unlink(in_path)
        if os.path.exists(out_path):
            os.unlink(out_path)


# ── Supabase helpers ──────────────────────────────────────────────────────────

def _sb_read_headers() -> dict:
    k = settings.supabase_anon_key
    return {"apikey": k, "Authorization": f"Bearer {k}", "Content-Type": "application/json"}


def _sb_write_headers() -> dict:
    k = settings.supabase_service_key
    return {"apikey": k, "Authorization": f"Bearer {k}", "Content-Type": "application/json"}


async def _get_row(http: httpx.AsyncClient, date_str: str) -> dict | None:
    """Fetch the full verse_of_the_day row for the given date."""
    if not settings.supabase_url or not settings.supabase_anon_key:
        return None
    r = await http.get(
        f"{settings.supabase_url}/rest/v1/verse_of_the_day",
        params={"date": f"eq.{date_str}", "select": "*"},
        headers=_sb_read_headers(),
    )
    rows = r.json() if r.status_code == 200 else []
    return rows[0] if rows else None


async def _mark_generating(http: httpx.AsyncClient, date_str: str) -> None:
    """Inserts/updates today's row to audio_status='generating'. Column defaults fill the rest."""
    r = await http.post(
        f"{settings.supabase_url}/rest/v1/verse_of_the_day",
        json={"date": date_str, "audio_status": "generating"},
        headers={**_sb_write_headers(), "Prefer": "resolution=merge-duplicates"},
    )
    if r.status_code not in (200, 201, 204):
        raise HTTPException(status_code=502, detail=f"Supabase mark-generating failed {r.status_code}: {r.text[:200]}")


async def _upsert_verse(
    http: httpx.AsyncClient,
    date_str: str, ref: str, book: str,
    chapter: int, verse: int, text: str,
    audio_status: str = "generating",
) -> None:
    """Writes the real verse data into the row (overwrites placeholder defaults)."""
    r = await http.post(
        f"{settings.supabase_url}/rest/v1/verse_of_the_day",
        json={
            "date": date_str, "verse_ref": ref, "book": book,
            "chapter": chapter, "verse": verse,
            "verse_text": text, "translation": "NIV",
            "audio_status": audio_status,
        },
        headers={**_sb_write_headers(), "Prefer": "resolution=merge-duplicates"},
    )
    if r.status_code not in (200, 201, 204):
        raise HTTPException(status_code=502, detail=f"Supabase verse upsert failed {r.status_code}: {r.text[:300]}")


async def _upload_audio(http: httpx.AsyncClient, date_str: str, mp3_bytes: bytes) -> str:
    import time
    path = f"{date_str}.mp3"
    r = await http.post(
        f"{settings.supabase_url}/storage/v1/object/{AUDIO_BUCKET}/{path}",
        content=mp3_bytes,
        headers={
            "apikey": settings.supabase_service_key,
            "Authorization": f"Bearer {settings.supabase_service_key}",
            "Content-Type": "audio/mpeg",
            "x-upsert": "true",
        },
    )
    if r.status_code not in (200, 201):
        raise HTTPException(status_code=502, detail=f"Storage upload failed {r.status_code}: {r.text[:300]}")
    # Append a version timestamp so iOS/CDN cache is busted on every regeneration
    v = int(time.time())
    return f"{settings.supabase_url}/storage/v1/object/public/{AUDIO_BUCKET}/{path}?v={v}"


async def _update_status(
    http: httpx.AsyncClient,
    date_str: str,
    audio_url: str | None,
    status: str,
) -> None:
    payload: dict = {"audio_status": status}
    if audio_url:
        payload["audio_url"] = audio_url
    r = await http.patch(
        f"{settings.supabase_url}/rest/v1/verse_of_the_day",
        json=payload,
        params={"date": f"eq.{date_str}"},
        headers=_sb_write_headers(),
    )
    if r.status_code not in (200, 201, 204):
        raise HTTPException(status_code=502, detail=f"Supabase status update failed {r.status_code}: {r.text[:300]}")


# ── Utility ───────────────────────────────────────────────────────────────────

def _book_name(verse_ref: str) -> str:
    """Extracts 'Romans' from 'Romans 8:28'."""
    parts = verse_ref.rsplit(" ", 1)
    return parts[0] if len(parts) == 2 else verse_ref
