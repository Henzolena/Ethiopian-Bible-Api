"""
Verse of the Day — Audio Generation Router
===========================================

POST /api/v1/votd/generate-audio  (protected by X-Admin-Key header)
GET  /api/v1/votd/today            (public)

Flow
----
  1. Fetch today's verse from this service's own GET /api/v1/en/votd endpoint.
  2. Mistral small writes a 180-200 word spoken devotional script.
  3. Gemini 2.5 Flash TTS converts the script to WAV audio.
  4. pydub converts WAV → MP3 @ 128 kbps (requires ffmpeg in the container).
  5. MP3 uploaded to Supabase Storage bucket "verse-audio" as {date}.mp3.
  6. Supabase verse_of_the_day row updated: audio_url + audio_status = "ready".

Required Railway environment variables
---------------------------------------
  SUPABASE_URL          https://bpqauxqpibaosnbvhito.supabase.co
  SUPABASE_SERVICE_KEY  <service_role key from Supabase dashboard → Settings → API>
  VOTD_ADMIN_KEY        <any secret string you choose>
  GEMINI_API_KEY        already set ✅
  MISTRAL_API_KEY       already set ✅
"""

from __future__ import annotations

import base64
import io
from datetime import date

import httpx
from fastapi import APIRouter, Header, HTTPException

from app.config import settings

router = APIRouter(prefix="/votd", tags=["Verse of the Day"])

AUDIO_BUCKET = "verse-audio"

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
    Trigger once per day (Railway cron or manual curl).
    """
    _check_admin(x_admin_key)
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
                          verse["chapter"], verse["verse"], text, "KJV", "generating")

        try:
            # 4. Mistral writes the devotional script.
            script = _generate_script(ref, text)

            # 5. Gemini TTS → WAV bytes.
            wav_bytes = _generate_audio_bytes(script)

            # 6. WAV → MP3.
            mp3_bytes = _wav_to_mp3(wav_bytes)

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


# ── Generation ────────────────────────────────────────────────────────────────

def _generate_script(ref: str, text: str) -> str:
    """Uses Mistral to write a 180-200 word spoken devotional."""
    from mistralai import Mistral  # type: ignore

    client = Mistral(api_key=settings.mistral_api_key)
    prompt = (
        "You are a warm, reverent biblical devotional speaker.\n"
        f"Write a 180-to-200-word spoken devotional about this verse:\n\n"
        f"{ref}: \"{text}\"\n\n"
        "Structure (spoken delivery only — no headers, no markdown):\n"
        "1. Read the verse naturally.\n"
        "2. Briefly explain its historical or biblical context (2–3 sentences).\n"
        "3. Share a practical, encouraging application for today (3–4 sentences).\n"
        "4. Close with a short prayer or blessing (1–2 sentences).\n\n"
        "Write ONLY the spoken text. No stage directions."
    )
    resp = client.chat.complete(
        model=settings.mistral_model,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=350,
        temperature=0.7,
    )
    return resp.choices[0].message.content.strip()


def _generate_audio_bytes(script: str) -> bytes:
    """Calls Gemini 2.5 Flash TTS and returns raw WAV bytes."""
    import google.generativeai as genai  # type: ignore
    from google.generativeai.types import (  # type: ignore
        GenerateContentConfig,
        PrebuiltVoiceConfig,
        SpeechConfig,
        VoiceConfig,
    )

    client = genai.Client(api_key=settings.gemini_api_key)
    response = client.models.generate_content(
        model="gemini-2.5-flash-preview-tts",
        contents=script,
        config=GenerateContentConfig(
            response_modalities=["AUDIO"],
            speech_config=SpeechConfig(
                voice_config=VoiceConfig(
                    prebuilt_voice_config=PrebuiltVoiceConfig(
                        voice_name="Kore"   # warm, clear, reverent
                    )
                )
            ),
        ),
    )
    b64 = response.candidates[0].content.parts[0].inline_data.data
    return base64.b64decode(b64)


def _wav_to_mp3(wav_bytes: bytes) -> bytes:
    """Converts WAV bytes → MP3 @ 128 kbps. Requires ffmpeg in the container."""
    from pydub import AudioSegment  # type: ignore

    seg = AudioSegment.from_wav(io.BytesIO(wav_bytes))
    buf = io.BytesIO()
    seg.export(buf, format="mp3", bitrate="128k")
    return buf.getvalue()


# ── Supabase helpers ──────────────────────────────────────────────────────────

def _sb_headers() -> dict:
    return {
        "apikey":        settings.supabase_service_key,
        "Authorization": f"Bearer {settings.supabase_service_key}",
        "Content-Type":  "application/json",
    }


async def _fetch_votd(http: httpx.AsyncClient) -> dict | None:
    """Fetches today's English verse from this service's own /en/votd endpoint."""
    try:
        r = await http.get("http://localhost:8000/api/v1/en/votd", timeout=10)
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None


async def _get_supabase_row(http: httpx.AsyncClient, date_str: str) -> dict | None:
    r = await http.get(
        f"{settings.supabase_url}/rest/v1/verse_of_the_day",
        params={"date": f"eq.{date_str}", "select": "audio_url,audio_status"},
        headers=_sb_headers(),
    )
    rows = r.json() if r.status_code == 200 else []
    return rows[0] if rows else None


async def _upsert_row(
    http: httpx.AsyncClient,
    date_str: str, ref: str, book: str,
    chapter: int, verse: int, text: str,
    translation: str, status: str,
) -> None:
    await http.post(
        f"{settings.supabase_url}/rest/v1/verse_of_the_day",
        json={
            "date": date_str, "verse_ref": ref, "book": book,
            "chapter": chapter, "verse": verse,
            "verse_text": text, "translation": translation,
            "audio_status": status,
        },
        headers={**_sb_headers(), "Prefer": "resolution=merge-duplicates"},
    )


async def _upload_audio(http: httpx.AsyncClient, date_str: str, mp3_bytes: bytes) -> str:
    path = f"{date_str}.mp3"
    await http.post(
        f"{settings.supabase_url}/storage/v1/object/{AUDIO_BUCKET}/{path}",
        content=mp3_bytes,
        headers={
            "Authorization": f"Bearer {settings.supabase_service_key}",
            "Content-Type":  "audio/mpeg",
            "x-upsert":      "true",
        },
    )
    return f"{settings.supabase_url}/storage/v1/object/public/{AUDIO_BUCKET}/{path}"


async def _update_row(
    http: httpx.AsyncClient,
    date_str: str,
    audio_url: str | None,
    status: str,
) -> None:
    payload: dict = {"audio_status": status}
    if audio_url:
        payload["audio_url"] = audio_url
    await http.patch(
        f"{settings.supabase_url}/rest/v1/verse_of_the_day",
        json=payload,
        params={"date": f"eq.{date_str}"},
        headers=_sb_headers(),
    )
