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
from fastapi import APIRouter, Header, HTTPException

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
                          verse["chapter"], verse["verse"], text, "KJV", "generating")

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


# ── AI generation (raw httpx — no SDKs) ──────────────────────────────────────

async def _generate_script(ref: str, text: str, http: httpx.AsyncClient) -> str:
    """Calls Mistral chat-completions to write a ~200-word spoken devotional."""
    prompt = (
        f"Write a 180-to-200-word spoken biblical devotional about this verse:\n\n"
        f"{ref}: \"{text}\"\n\n"
        "Structure (spoken delivery — no headers, no markdown, no stage directions):\n"
        "1. Read the verse naturally.\n"
        "2. Explain its historical or biblical context (2-3 sentences).\n"
        "3. Share a practical, encouraging application for today (3-4 sentences).\n"
        "4. Close with a short prayer or blessing (1-2 sentences).\n\n"
        "Write ONLY the spoken text."
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
    The audio is typically PCM/WAV — pydub converts it to MP3 downstream.
    """
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_TTS_MODEL}:generateContent?key={settings.gemini_api_key}"
    )

    resp = await http.post(
        url,
        json={
            "contents": [{"parts": [{"text": script}]}],
            "generationConfig": {
                "responseModalities": ["AUDIO"],
                "speechConfig": {
                    "voiceConfig": {
                        "prebuiltVoiceConfig": {"voiceName": "Kore"}
                    }
                },
            },
        },
        timeout=90,
    )

    if resp.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"Gemini TTS error {resp.status_code}: {resp.text[:300]}",
        )

    data      = resp.json()
    part      = data["candidates"][0]["content"]["parts"][0]["inlineData"]
    mime_type = part.get("mimeType", "audio/wav")
    raw_bytes = base64.b64decode(part["data"])
    return raw_bytes, mime_type


def _to_mp3(audio_bytes: bytes, mime_type: str) -> bytes:
    """Converts Gemini audio output (WAV or raw PCM) → MP3 @ 128 kbps via pydub."""
    from pydub import AudioSegment  # requires ffmpeg in container

    buf_in = io.BytesIO(audio_bytes)

    if "wav" in mime_type.lower():
        seg = AudioSegment.from_wav(buf_in)
    elif "mp3" in mime_type.lower():
        return audio_bytes   # already MP3 — pass through
    else:
        # Raw linear16 PCM at 24 kHz, mono (Gemini TTS default)
        seg = AudioSegment.from_raw(buf_in, sample_width=2, frame_rate=24000, channels=1)

    buf_out = io.BytesIO()
    seg.export(buf_out, format="mp3", bitrate="128k")
    return buf_out.getvalue()


# ── Supabase helpers ──────────────────────────────────────────────────────────

def _sb_read_headers() -> dict:
    key = settings.supabase_anon_key
    return {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def _sb_write_headers() -> dict:
    key = settings.supabase_service_key
    return {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"}


async def _fetch_votd(http: httpx.AsyncClient) -> dict | None:
    try:
        r = await http.get("http://localhost:8000/api/v1/en/votd", timeout=10)
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
    await http.post(
        f"{settings.supabase_url}/rest/v1/verse_of_the_day",
        json={
            "date": date_str, "verse_ref": ref, "book": book,
            "chapter": chapter, "verse": verse,
            "verse_text": text, "translation": translation,
            "audio_status": status,
        },
        headers={**_sb_write_headers(), "Prefer": "resolution=merge-duplicates"},
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
        headers=_sb_write_headers(),
    )
