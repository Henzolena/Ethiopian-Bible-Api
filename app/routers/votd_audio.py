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

import asyncio
import base64
import io
import json
from datetime import date

import httpx
from fastapi import APIRouter, BackgroundTasks, Header, HTTPException

from app.config import settings

router = APIRouter(prefix="/votd", tags=["Verse of the Day"])

AUDIO_BUCKET    = "verse-audio"
GEMINI_TTS_MODEL = "gemini-2.5-flash-preview-tts"


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

    verse = None
    if row and row.get("verse_ref") and row.get("verse_text"):
        verse = {
            "book":      row.get("book", ""),
            "book_name": _book_name(row.get("verse_ref", "")),
            "chapter":   row.get("chapter", 0),
            "verse":     row.get("verse", 0),
            "text":      row.get("verse_text", ""),
            "language":  "niv",
        }

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
    # 1. Fetch all previous verse refs for deduplication
    used_refs = await _get_used_refs(http)

    # 2. Mistral selects today's verse
    selection = await _select_verse(http, used_refs)
    if not selection:
        raise HTTPException(status_code=502, detail="AI could not select a verse — check Mistral API key")

    # 3. Fetch NIV verse text from Bible API
    verse = await _fetch_verse_niv(http, selection["book"], selection["chapter"], selection["verse"])
    if not verse:
        raise HTTPException(
            status_code=502,
            detail=f"Could not fetch NIV verse {selection['book']} {selection['chapter']}:{selection['verse']}",
        )

    ref  = f"{verse['book_name']} {verse['chapter']}:{verse['verse']}"
    text = verse["text"]

    # 4. Persist the real verse info (overwrites placeholder defaults)
    await _upsert_verse(http, today_str, ref, verse["book"],
                        verse["chapter"], verse["verse"], text)

    # 5. Generate devotional script
    script = await _generate_script(ref, text, http)

    # 6. Gemini TTS → audio bytes
    audio_bytes, mime_type = await _generate_audio(script, http)

    # 7. Convert to MP3
    mp3_bytes = _to_mp3(audio_bytes, mime_type)

    # 8. Upload to Supabase Storage
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


async def _select_verse(http: httpx.AsyncClient, used_refs: list[str]) -> dict | None:
    """
    Uses Mistral to select a spiritually rich, standalone NIV verse for today.
    Returns {"book": "ROM", "chapter": 8, "verse": 28} or None on failure.
    """
    used_list = "\n".join(f"  - {r}" for r in used_refs) if used_refs else "  (none yet)"

    prompt = (
        "You are a spiritual devotional curator. Select the perfect Bible verse "
        "for today's morning Verse of the Day.\n\n"
        "The verse MUST:\n"
        "  ✓ Stand completely alone — fully understood without any surrounding context\n"
        "  ✓ Contain a clear, complete spiritual truth, promise, or encouragement\n"
        "  ✓ Help the reader grow in faith, hope, love, or trust in God\n"
        "  ✓ Be meaningful and applicable to everyday Christian life\n"
        "  ✓ Come from a wide variety of Scripture (vary between OT and NT, Psalms, "
        "Proverbs, Gospels, Epistles, Prophets, etc.)\n\n"
        "The verse MUST NOT:\n"
        "  ✗ Be a mid-narrative verse that requires surrounding verses to make sense\n"
        "  ✗ Be purely historical, genealogical, or census-type content\n"
        "  ✗ Start with 'and', 'but', 'so', 'therefore', or 'then' — these signal "
        "mid-thought fragments with no standalone meaning\n"
        "  ✗ Name people or places without containing a timeless spiritual principle\n"
        "  ✗ Have already been used (list below)\n\n"
        f"Previously used verses — DO NOT select any of these:\n{used_list}\n\n"
        "Respond with ONLY a JSON object on a single line — no explanation, no markdown:\n"
        "{\"book\": \"ROM\", \"chapter\": 8, \"verse\": 28}\n\n"
        "Book abbreviations (use exactly as shown): "
        "GEN EXO LEV NUM DEU JOS JDG RUT 1SA 2SA 1KI 2KI 1CH 2CH EZR NEH EST JOB "
        "PSA PRO ECC SOS ISA JER LAM EZK DAN HOS JOL AMO OBA JON MIC NAH HAB ZEP "
        "HAG ZEC MAL MAT MRK LUK JHN ACT ROM 1CO 2CO GAL EPH PHP COL 1TH 2TH 1TI "
        "2TI TIT PHM HEB JAS 1PE 2PE 1JN 2JN 3JN JUD REV"
    )

    resp = await http.post(
        f"{settings.mistral_base_url}/chat/completions",
        headers={"Authorization": f"Bearer {settings.mistral_api_key}", "Content-Type": "application/json"},
        json={
            "model":           settings.mistral_model,
            "messages":        [{"role": "user", "content": prompt}],
            "response_format": {"type": "json_object"},
            "temperature":     0.85,   # enough variety to prevent repetition
            "max_tokens":      30,
        },
        timeout=15,
    )

    if resp.status_code != 200:
        print(f"[VOTD] verse selection failed {resp.status_code}: {resp.text[:200]}")
        return None

    try:
        content = resp.json()["choices"][0]["message"]["content"]
        sel = json.loads(content)
        if all(k in sel for k in ("book", "chapter", "verse")):
            return {"book": str(sel["book"]), "chapter": int(sel["chapter"]), "verse": int(sel["verse"])}
    except Exception as exc:
        print(f"[VOTD] verse selection parse error: {exc} — raw: {content[:200]}")
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

async def _generate_script(ref: str, text: str, http: httpx.AsyncClient) -> str:
    """Calls Mistral to write a 180-200 word spoken NIV devotional."""
    prompt = (
        "You are a warm, Spirit-filled biblical devotional speaker — "
        "like a trusted pastor speaking directly to someone beginning their morning.\n\n"
        f"Today's Verse of the Day (NIV) — {ref}:\n\"{text}\"\n\n"
        "Write a 180-200 word spoken devotional in exactly this structure:\n\n"
        "1. READ — speak the verse once, naturally and clearly.\n"
        "2. ILLUMINATE (2-3 sentences) — unpack what God is saying in this verse simply. "
        "Who wrote it, to whom, and what is the core spiritual truth? "
        "Speak to the heart, not the head — no academic language.\n"
        "3. APPLY (3-4 sentences) — bring this verse into today. "
        "What does God want this specific person to feel, believe, or do? "
        "Be warm, personal, and specific to THIS verse — no generic filler.\n"
        "4. BLESS (1-2 sentences) — close with a sincere prayer or blessing "
        "the listener carries into their day.\n\n"
        "Rules:\n"
        "- Modern NIV-level English — clear, never archaic\n"
        "- Speak to ONE person directly\n"
        "- Every sentence must matter — no repetition, no clichés\n"
        "- Write ONLY the spoken text. No headers, no bullet points."
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

    return resp.json()["choices"][0]["message"]["content"].strip()


# ── AI: Gemini TTS ────────────────────────────────────────────────────────────

async def _generate_audio(script: str, http: httpx.AsyncClient) -> tuple[bytes, str]:
    """Calls Gemini TTS. Retries up to 3× on 429."""
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_TTS_MODEL}:generateContent?key={settings.gemini_api_key}"
    )
    payload = {
        "contents": [{"parts": [{"text": script}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": "Kore"}}},
        },
    }

    for attempt in range(1, 4):
        resp = await http.post(url, json=payload, timeout=90)
        if resp.status_code == 200:
            part      = resp.json()["candidates"][0]["content"]["parts"][0]["inlineData"]
            mime_type = part.get("mimeType", "audio/wav")
            return base64.b64decode(part["data"]), mime_type
        if resp.status_code == 429 and attempt < 3:
            wait = 15 * attempt
            print(f"[VOTD] Gemini TTS 429 — waiting {wait}s (attempt {attempt+1}/3)")
            await asyncio.sleep(wait)
            continue
        raise HTTPException(status_code=502, detail=f"Gemini TTS error {resp.status_code}: {resp.text[:300]}")

    raise HTTPException(status_code=502, detail="Gemini TTS rate-limited after 3 attempts")


def _to_mp3(audio_bytes: bytes, mime_type: str) -> bytes:
    """Converts audio bytes → MP3 via ffmpeg (Python 3.13-safe, no pydub)."""
    import os, subprocess, tempfile

    if "mp3" in mime_type.lower():
        return audio_bytes

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
) -> None:
    """Writes the real verse data into the row (overwrites placeholder defaults)."""
    r = await http.post(
        f"{settings.supabase_url}/rest/v1/verse_of_the_day",
        json={
            "date": date_str, "verse_ref": ref, "book": book,
            "chapter": chapter, "verse": verse,
            "verse_text": text, "translation": "NIV",
            "audio_status": "generating",
        },
        headers={**_sb_write_headers(), "Prefer": "resolution=merge-duplicates"},
    )
    if r.status_code not in (200, 201, 204):
        raise HTTPException(status_code=502, detail=f"Supabase verse upsert failed {r.status_code}: {r.text[:300]}")


async def _upload_audio(http: httpx.AsyncClient, date_str: str, mp3_bytes: bytes) -> str:
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
    return f"{settings.supabase_url}/storage/v1/object/public/{AUDIO_BUCKET}/{path}"


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
