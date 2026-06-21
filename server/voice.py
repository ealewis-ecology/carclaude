"""Server-side speech: STT (Whisper / Deepgram) and TTS (OpenAI / ElevenLabs).

Claude has no audio capabilities, so transcription and synthesis are done by a
separate provider here. All calls are plain HTTP via httpx (no provider SDKs), so
swapping providers is just config. Keys live on the Pi; the phone never sees them.
"""
from __future__ import annotations

import re

import httpx

from server.config import settings
from server import prefs, state

_TIMEOUT = httpx.Timeout(60.0, connect=10.0)


class VoiceError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# Speech -> text                                                              #
# --------------------------------------------------------------------------- #
async def transcribe(audio: bytes, content_type: str = "audio/webm") -> str:
    if not audio:
        return ""
    if settings.stt_provider == "deepgram":
        return await _stt_deepgram(audio, content_type)
    return await _stt_openai(audio, content_type)


async def _stt_openai(audio: bytes, content_type: str) -> str:
    if not settings.openai_api_key:
        raise VoiceError("OPENAI_API_KEY is not set (needed for Whisper STT).")
    ext = _ext_for(content_type)
    data = {"model": settings.stt_model, "response_format": "json"}
    lang = prefs.load()["stt_language"]
    if lang:
        data["language"] = lang
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        r = await client.post(
            "https://api.openai.com/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {settings.openai_api_key}"},
            data=data,
            files={"file": (f"speech.{ext}", audio, content_type)},
        )
    if r.status_code >= 400:
        raise VoiceError(f"Whisper STT failed ({r.status_code}): {r.text[:300]}")
    return (r.json().get("text") or "").strip()


async def _stt_deepgram(audio: bytes, content_type: str) -> str:
    if not settings.deepgram_api_key:
        raise VoiceError("DEEPGRAM_API_KEY is not set.")
    params = {"model": settings.deepgram_model, "smart_format": "true", "punctuate": "true"}
    lang = prefs.load()["stt_language"]
    if lang:
        params["language"] = lang
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        r = await client.post(
            "https://api.deepgram.com/v1/listen",
            params=params,
            headers={"Authorization": f"Token {settings.deepgram_api_key}",
                     "Content-Type": content_type or "audio/webm"},
            content=audio,
        )
    if r.status_code >= 400:
        raise VoiceError(f"Deepgram STT failed ({r.status_code}): {r.text[:300]}")
    try:
        alt = r.json()["results"]["channels"][0]["alternatives"][0]
        return (alt.get("transcript") or "").strip()
    except (KeyError, IndexError) as e:
        raise VoiceError(f"Deepgram response unexpected: {e}")


# --------------------------------------------------------------------------- #
# Text -> speech  (returns (audio_bytes, mime_type))                           #
# --------------------------------------------------------------------------- #
async def synthesize(text: str) -> tuple[bytes, str]:
    text = (text or "").strip()
    if not text:
        return b"", "audio/mpeg"
    if settings.tts_provider == "elevenlabs":
        return await _tts_elevenlabs(text), "audio/mpeg"
    return await _tts_openai(text), "audio/mpeg"


async def _tts_openai(text: str) -> bytes:
    if not settings.openai_api_key:
        raise VoiceError("OPENAI_API_KEY is not set (needed for TTS).")
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        r = await client.post(
            "https://api.openai.com/v1/audio/speech",
            headers={"Authorization": f"Bearer {settings.openai_api_key}"},
            json={"model": settings.tts_model, "voice": settings.tts_voice,
                  "input": text, "response_format": "mp3"},
        )
    if r.status_code >= 400:
        raise VoiceError(f"OpenAI TTS failed ({r.status_code}): {r.text[:300]}")
    return r.content


async def _tts_elevenlabs(text: str) -> bytes:
    p = prefs.load()
    voice_id = state.voice_id or settings.elevenlabs_voice_id   # active persona's voice
    if not settings.elevenlabs_api_key or not voice_id:
        raise VoiceError("ELEVENLABS_API_KEY / voice id not set.")
    if not re.fullmatch(r"[A-Za-z0-9]+", voice_id):  # don't let a persona's voice_id traverse the API path
        raise VoiceError("Invalid persona voice_id (expected alphanumeric).")
    body = {"text": text, "model_id": p["tts_model"]}
    if p["speech_rate"] != 1.0:
        body["voice_settings"] = {"speed": p["speech_rate"]}
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        r = await client.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
            headers={"xi-api-key": settings.elevenlabs_api_key, "accept": "audio/mpeg"},
            json=body,
        )
    if r.status_code >= 400:
        raise VoiceError(f"ElevenLabs TTS failed ({r.status_code}): {r.text[:300]}")
    return r.content


def _ext_for(content_type: str) -> str:
    ct = (content_type or "").lower()
    if "mp4" in ct or "m4a" in ct or "aac" in ct:
        return "m4a"
    if "mpeg" in ct or "mp3" in ct:
        return "mp3"
    if "wav" in ct:
        return "wav"
    if "ogg" in ct:
        return "ogg"
    return "webm"
