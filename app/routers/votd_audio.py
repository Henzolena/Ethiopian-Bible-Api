"""
Verse of the Day — Audio Generation Router
===========================================

POST /api/v1/votd/generate-audio  (protected by X-Admin-Key header)
GET  /api/v1/votd/today            (public)

Flow
----
  1. Fetch today's verse from this service's own GET /api/v1/en/votd.
  2. Mistral chat-completions (OpenAI-compatible API via httpx) writes
     a 180-200 word spoken devotional script.
  3. Gemini 2.5 Flash Preview TTS REST API converts the script to audio.
  4. pydub converts WAV/PCM → MP3 @ 128 kbps (ffmpeg in container).
  5. MP3 uploaded to Supabase Storage bucket "verse-audio" as {date}.mp3.
  6. Supabase verse_of_the_day row updated: audio_url + status = "ready".

Uses only httpx for all external API calls — no Python AI SDKs required.

Required Railway environment variables
---------------------------------------
  SUPABASE_URL          https://bpqauxqpibaosnbvhito.supabase.co
  SUPABASE_ANON_KEY     anon/publishable key  (public reads)
  SUPABASE_SERVICE_KEY  service_role key      (DB writes + Storage uploads)
  VOTD_ADMIN_KEY        any secret string you choose
  GEMINI_API_KEY        already set ✅
  MISTRAL_API_KEY       already set ✅
"""

from __future__ import annotations

import base64
import io
from datetime import date

import httpx
from fastapi import APIRouter, BackgroundTasks, Header, HTTPException

from app.config import settings

router = APIRouter(prefix="/votd", tags=["Verse of the Day"])

AUDIO_BUCKET = "verse-audio"
GEMINI_TTS_MODEL = "gemini-2.5-flash-preview-tts"


# ── Auth ──────────────────────────────────────────────────────────────────────

def _check_admin(key: str | None) -> None:
    if not settings.votd_admin_key or key != settings.votd_admin_key:
        raise HTTPException(status_code=403, detail="Invalid or missing X-Admin-Key")


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("/generate-audio")
async def generate_votd_audio(x_admin_key: str | None = Header(None, alias="X-Admin-Key")):
    """
    Generates and stores devotional audio for today's verse.
    Idempotent — returns early if audio is already ready.
    Requires SUPABASE_SERVICE_KEY (service_role key from Supabase → Settings → API).
    """
    _check_admin(x_admin_key)

    if not settings.supabase_url or not settings.supabase_service_key:
        raise HTTPException(
            status_code=503,
            detail=(
                "SUPABASE_URL and SUPABASE_SERVICE_KEY must be set in Railway Variables. "
                "Get the service_role key from Supabase dashboard → Settings → API."
            ),
        )

    today_str = date.today().isoformat()

    async with httpx.AsyncClient(timeout=120) as http:

        # 1. Already generated today? Return early.
        existing = await _get_supabase_row(http, today_str)
        if existing and existing.get("audio_status") == "ready":
            return {
                "status":    "already_exists",
                "audio_url": existing["audio_url"],
                "date":      today_str,
            }

        # 2. Fetch today's verse text.
        verse = await _fetch_votd(http)
        if not verse:
            raise HTTPException(status_code=503, detail="Could not fetch verse of the day from /en/votd")

        ref  = f"{verse['book_name']} {verse['chapter']}:{verse['verse']}"
        text = verse["text"]

        # 3. Mark row as "generating" so concurrent calls bail early.
        await _upsert_row(http, today_str, ref, verse["book"],
                          verse["chapter"], verse["verse"], text, "NIV", "generating")

        try:
            # 4. Mistral writes the devotional script (plain text, no JSON).
            script = await _generate_script(ref, text, http)

            # 5. Gemini TTS → raw audio bytes + mime type.
            audio_bytes, mime_type = await _generate_audio(script, http)

            # 6. Convert to MP3.
            mp3_bytes = _to_mp3(audio_bytes, mime_type)

            # 7. Upload to Supabase Storage.
            audio_url = await _upload_audio(http, today_str, mp3_bytes)

            # 8. Persist URL and mark ready.
            await _update_row(http, today_str, audio_url, "ready")

            return {
                "status":         "ok",
                "audio_url":      audio_url,
                "date":           today_str,
                "script_preview": script[:120] + "…",
            }

        except HTTPException:
            await _update_row(http, today_str, None, "failed")
            raise
        except Exception as exc:
            await _update_row(http, today_str, None, "failed")
            raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/today")
async def get_today():
    """Returns today's verse text + audio metadata (no auth required)."""
    today_str = date.today().isoformat()
    async with httpx.AsyncClient(timeout=15) as http:
        verse = await _fetch_votd(http)
        row   = await _get_supabase_row(http, today_str)
    return {
        "verse":        verse,
        "audio_url":    row.get("audio_url")    if row else None,
        "audio_status": row.get("audio_status") if row else "pending",
        "date":         today_str,
    }


@router.post("/request-audio")
async def request_votd_audio(background_tasks: BackgroundTasks):
    """
    Public endpoint — iOS calls this on first play tap when audio isn't ready.
    Returns immediately with the current status.
    If status is pending/failed, marks row as generating and kicks off the
    full pipeline (Mistral + Gemini TTS + upload) as a background task.
    iOS should poll GET /today every 5 seconds until audio_status == "ready".
    """
    if not settings.supabase_url or not settings.supabase_service_key:
        return {"status": "unavailable"}

    today_str = date.today().isoformat()

    async with httpx.AsyncClient(timeout=10) as http:
        row = await _get_supabase_row(http, today_str)

        # Already done — return immediately
        if row and row.get("audio_status") == "ready":
            return {"status": "ready", "audio_url": row.get("audio_url")}

        # Already in progress — don't spawn a second task
        if row and row.get("audio_status") == "generating":
            return {"status": "generating", "poll_interval_seconds": 5}

        # Mark as generating NOW (before returning) so concurrent taps are no-ops
        verse = await _fetch_votd(http)
        if not verse:
            return {"status": "unavailable"}

        ref = f"{verse['book_name']} {verse['chapter']}:{verse['verse']}"
        try:
            await _upsert_row(http, today_str, ref, verse["book"],
                              verse["chapter"], verse["verse"],
                              verse["text"], "NIV", "generating")
        except HTTPException:
            return {"status": "unavailable"}

    # Kick off full generation pipeline after the response is sent
    background_tasks.add_task(_run_generation_background, today_str)
    return {"status": "generating", "poll_interval_seconds": 5}


# ── AI generation (raw httpx — no SDKs) ──────────────────────────────────────

async def _generate_script(ref: str, text: str, http: httpx.AsyncClient) -> str:
    """Calls Mistral chat-completions to write a ~200-word spoken NIV devotional."""
    prompt = (
        "You are a warm, Spirit-filled biblical devotional speaker — like a trusted pastor "
        "sitting across the table from someone beginning their morning.\n\n"
        f"Today's verse (NIV) is {ref}:\n\"{text}\"\n\n"
        "Write a 180-200 word spoken devotional. Follow this exact structure:\n\n"
        "1. READ — speak the verse once, naturally and clearly, as you would say it aloud.\n"
        "2. ILLUMINATE (2-3 sentences) — unpack the heart of this verse simply: "
        "what God is communicating, who it was written to, and the spiritual truth at its core. "
        "No academic language — speak to the heart, not the head.\n"
        "3. APPLY (3-4 sentences) — bring this verse into today. "
        "What does God want this person to feel, believe, or do because of this truth? "
        "Be personal, warm, and specific to THIS verse — avoid generic phrases.\n"
        "4. BLESS (1-2 sentences) — close with a sincere, simple prayer or blessing "
        "the listener can carry into their day.\n\n"
        "Rules:\n"
        "- Use modern NIV-level English — clear, warm, never archaic\n"
        "- Speak to ONE person, not a crowd\n"
        "- Every sentence must earn its place — no filler, no clichés\n"
        "- Write ONLY the spoken text. No headers, no bullet points, no stage directions."
    )

    resp = await http.post(
        f"{settings.mistral_base_url}/chat/completions",
        headers={
            "Authorization": f"Bearer {settings.mistral_api_key}",
            "Content-Type":  "application/json",
        },
        json={
            "model":       settings.mistral_model,
            "messages":    [{"role": "user", "content": prompt}],
            "temperature": 0.7,
            "max_tokens":  400,
        },
        timeout=30,
    )

    if resp.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"Mistral API error {resp.status_code}: {resp.text[:200]}",
        )

    return resp.json()["choices"][0]["message"]["content"].strip()


async def _generate_audio(script: str, http: httpx.AsyncClient) -> tuple[bytes, str]:
    """
    Calls Gemini 2.5 Flash Preview TTS via REST and returns (audio_bytes, mime_type).
    Retries up to 3 times with backoff on 429 rate-limit responses.
    """
    import asyncio

    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_TTS_MODEL}:generateContent?key={settings.gemini_api_key}"
    )
    payload = {
        "contents": [{"parts": [{"text": script}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {
                "voiceConfig": {
                    "prebuiltVoiceConfig": {"voiceName": "Kore"}
                }
            },
        },
    }

    for attempt in range(1, 4):
        resp = await http.post(url, json=payload, timeout=90)

        if resp.status_code == 200:
            data      = resp.json()
            part      = data["candidates"][0]["content"]["parts"][0]["inlineData"]
            mime_type = part.get("mimeType", "audio/wav")
            raw_bytes = base64.b64decode(part["data"])
            return raw_bytes, mime_type

        if resp.status_code == 429 and attempt < 3:
            wait = 15 * attempt   # 15s, 30s
            print(f"[VOTD] Gemini TTS 429 — waiting {wait}s before retry {attempt+1}/3")
            await asyncio.sleep(wait)
            continue

        raise HTTPException(
            status_code=502,
            detail=f"Gemini TTS error {resp.status_code}: {resp.text[:300]}",
        )

    raise HTTPException(status_code=502, detail="Gemini TTS rate-limited after 3 attempts")


def _to_mp3(audio_bytes: bytes, mime_type: str) -> bytes:
    """
    Converts Gemini audio output → MP3 @ 128 kbps using ffmpeg directly.
    Avoids pydub (broken on Python 3.13 — pyaudioop removed from stdlib).
    ffmpeg is installed in the container via Dockerfile.
    """
    import os
    import subprocess
    import tempfile

    if "mp3" in mime_type.lower():
        return audio_bytes  # already MP3 — pass through

    # Write input to a temp file, let ffmpeg auto-detect format
    with tempfile.NamedTemporaryFile(delete=False, suffix=".input") as f:
        f.write(audio_bytes)
        in_path = f.name

    out_path = in_path + ".mp3"
    try:
        # For raw PCM (Gemini TTS default: linear16, 24 kHz, mono)
        if "wav" in mime_type.lower():
            cmd = ["ffmpeg", "-y", "-i", in_path, "-b:a", "128k", out_path]
        else:
            cmd = [
                "ffmpeg", "-y",
                "-f", "s16le", "-ar", "24000", "-ac", "1",
                "-i", in_path,
                "-b:a", "128k", out_path,
            ]
        result = subprocess.run(cmd, capture_output=True, timeout=30)
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg failed: {result.stderr.decode()[-300:]}")
        with open(out_path, "rb") as fmp3:
            return fmp3.read()
    finally:
        os.unlink(in_path)
        if os.path.exists(out_path):
            os.unlink(out_path)


# ── Supabase helpers ──────────────────────────────────────────────────────────

def _sb_read_headers() -> dict:
    key = settings.supabase_anon_key
    return {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def _sb_write_headers() -> dict:
    key = settings.supabase_service_key
    return {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"}


async def _fetch_votd(http: httpx.AsyncClient) -> dict | None:
    try:
        r = await http.get("http://localhost:8000/api/v1/niv/votd", timeout=10)
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None


async def _get_supabase_row(http: httpx.AsyncClient, date_str: str) -> dict | None:
    if not settings.supabase_url or not settings.supabase_anon_key:
        return None
    r = await http.get(
        f"{settings.supabase_url}/rest/v1/verse_of_the_day",
        params={"date": f"eq.{date_str}", "select": "audio_url,audio_status"},
        headers=_sb_read_headers(),
    )
    rows = r.json() if r.status_code == 200 else []
    return rows[0] if rows else None


async def _upsert_row(
    http: httpx.AsyncClient,
    date_str: str, ref: str, book: str,
    chapter: int, verse: int, text: str,
    translation: str, status: str,
) -> None:
    r = await http.post(
        f"{settings.supabase_url}/rest/v1/verse_of_the_day",
        json={
            "date": date_str, "verse_ref": ref, "book": book,
            "chapter": chapter, "verse": verse,
            "verse_text": text, "translation": translation,
            "audio_status": status,
        },
        headers={**_sb_write_headers(), "Prefer": "resolution=merge-duplicates"},
    )
    if r.status_code not in (200, 201, 204):
        raise HTTPException(
            status_code=502,
            detail=f"Supabase upsert failed {r.status_code}: {r.text[:300]}",
        )


async def _upload_audio(http: httpx.AsyncClient, date_str: str, mp3_bytes: bytes) -> str:
    path = f"{date_str}.mp3"
    r = await http.post(
        f"{settings.supabase_url}/storage/v1/object/{AUDIO_BUCKET}/{path}",
        content=mp3_bytes,
        headers={
            "apikey":        settings.supabase_service_key,
            "Authorization": f"Bearer {settings.supabase_service_key}",
            "Content-Type":  "audio/mpeg",
            "x-upsert":      "true",
        },
    )
    if r.status_code not in (200, 201):
        raise HTTPException(
            status_code=502,
            detail=f"Supabase Storage upload failed {r.status_code}: {r.text[:300]}",
        )
    return f"{settings.supabase_url}/storage/v1/object/public/{AUDIO_BUCKET}/{path}"


async def _run_generation_background(today_str: str) -> None:
    """
    Full audio generation pipeline run as a FastAPI BackgroundTask.
    Called by request_votd_audio after returning the response to the client.
    """
    async with httpx.AsyncClient(timeout=120) as http:
        try:
            verse = await _fetch_votd(http)
            if not verse:
                await _update_row(http, today_str, None, "failed")
                return

            ref  = f"{verse['book_name']} {verse['chapter']}:{verse['verse']}"
            text = verse["text"]

            script                   = await _generate_script(ref, text, http)
            audio_bytes, mime_type   = await _generate_audio(script, http)
            mp3_bytes                = _to_mp3(audio_bytes, mime_type)
            audio_url                = await _upload_audio(http, today_str, mp3_bytes)
            await _update_row(http, today_str, audio_url, "ready")
        except Exception as exc:
            print(f"[VOTD background] generation failed: {exc}")
            try:
                async with httpx.AsyncClient(timeout=10) as h:
                    await _update_row(h, today_str, None, "failed")
            except Exception:
                pass


async def _update_row(
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
        raise HTTPException(
            status_code=502,
            detail=f"Supabase row update failed {r.status_code}: {r.text[:300]}",
        )
