"""Live-tunable, NON-SECRET preferences. The agent may edit preferences.json by voice;
secrets (.env) are never touched. Every field is validated/clamped on read, so a
mis-heard value can't break anything — it just falls back to a safe default.

Fields:
  model            Claude model id (allowlist + aliases like "sonnet"/"haiku")
  effort           low|medium|high|xhigh|max  (thinking depth)
  pause_ms         silence before a turn submits (VAD)         200..5000
  max_ms           max utterance length (VAD)                  3000..120000
  speech_rate      ElevenLabs TTS speed                        0.7..1.2
  notes            standing instructions appended to the persona prompt
  barge_sensitivity 1 (hard to interrupt) .. 10 (easy)
  speech_floor     min normalized RMS (0..1) counted as speech — the absolute VAD/barge-in
                   backstop under the adaptive noise floor. Lower (e.g. 0.012) for quiet
                   headphones; raise to harden against dead-silence false triggers.  0.0..0.2
  read_only        if true, the agent won't edit files or run commands
  max_words        hard cap on spoken reply length (0 = off)   0..1000
  max_turns        cap the agent's tool-loop per request (0 = SDK default) 0..100
  daily_budget_usd advisory daily Claude spend limit (0 = off)
  tts_model        ElevenLabs model ("fast"=flash, "quality"=turbo)
  stt_language     STT language hint, e.g. "en" ("" = auto)
  recall_turns     past turns auto-recalled at session start (0 = off)  0..30
  status_ack       speak a short acknowledgement while working (true/false)
  ack_thinking     also speak the thinking cue while only thinking, no tool yet (true/false).
                   When false (default) only tool actions cue; pure thinking stays silent.
  ack_delay_ms     how long the agent may think before the thinking cue sounds (0 = every turn)  0..15000
  ack_phrase       the cue spoken while only thinking, no tool yet (short; persona voice). Tool
                   cues ("Reading files." etc.) are automatic and not configurable here.
"""
from __future__ import annotations

import json

from server.config import PROJECT_ROOT, settings

_FILE = PROJECT_ROOT / "preferences.json"
_EFFORTS = {"low", "medium", "high", "xhigh", "max"}
_MODELS = {"claude-fable-5", "claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6",
           "claude-sonnet-4-6", "claude-haiku-4-5"}
_MODEL_ALIASES = {"fable": "claude-fable-5", "opus": "claude-opus-4-8",
                  "sonnet": "claude-sonnet-4-6", "haiku": "claude-haiku-4-5"}
_TTS_MODELS = {"eleven_turbo_v2_5", "eleven_flash_v2_5", "eleven_multilingual_v2"}
_TTS_ALIASES = {"fast": "eleven_flash_v2_5", "flash": "eleven_flash_v2_5",
                "quality": "eleven_turbo_v2_5", "turbo": "eleven_turbo_v2_5"}


def _clamp(v, lo, hi, d) -> int:
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return d


def _clampf(v, lo, hi, d) -> float:
    try:
        return max(lo, min(hi, float(v)))
    except (TypeError, ValueError):
        return d


def _bool(v, d) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("true", "1", "yes", "on")
    return d


def _pick(raw, options, aliases, default):
    if not raw:
        return default
    s = str(raw).strip().lower()
    if s in options:
        return s
    if s in aliases:
        return aliases[s]
    for k, v in aliases.items():
        if k in s:
            return v
    return default


def load() -> dict:
    d = {
        "model": settings.agent_model,
        "effort": settings.agent_effort if settings.agent_effort in _EFFORTS else "medium",
        "pause_ms": 1000, "max_ms": 30000,
        "speech_rate": 1.0, "notes": "", "barge_sensitivity": 5, "speech_floor": 0.012,
        "read_only": False, "max_words": 0, "max_turns": 0, "daily_budget_usd": 0.0,
        "tts_model": settings.elevenlabs_model or "eleven_turbo_v2_5", "stt_language": "",
        "recall_turns": 6,
        "status_ack": True, "ack_thinking": False, "ack_delay_ms": 2500, "ack_phrase": "Thinking.",
    }
    try:
        raw = json.loads(_FILE.read_text())
    except (OSError, ValueError):
        raw = {}
    if isinstance(raw, dict):
        d["model"] = _pick(raw.get("model"), _MODELS, _MODEL_ALIASES, d["model"])
        e = str(raw.get("effort", "")).lower()
        if e in _EFFORTS:
            d["effort"] = e
        d["pause_ms"] = _clamp(raw.get("pause_ms"), 200, 5000, d["pause_ms"])
        d["max_ms"] = _clamp(raw.get("max_ms"), 3000, 120000, d["max_ms"])
        d["speech_rate"] = round(_clampf(raw.get("speech_rate"), 0.7, 1.2, 1.0), 2)
        if isinstance(raw.get("notes"), str):
            d["notes"] = raw["notes"][:2000]
        d["barge_sensitivity"] = _clamp(raw.get("barge_sensitivity"), 1, 10, 5)
        d["speech_floor"] = round(_clampf(raw.get("speech_floor"), 0.0, 0.2, 0.012), 4)
        d["read_only"] = _bool(raw.get("read_only"), False)
        d["max_words"] = _clamp(raw.get("max_words"), 0, 1000, 0)
        d["max_turns"] = _clamp(raw.get("max_turns"), 0, 100, 0)
        d["recall_turns"] = _clamp(raw.get("recall_turns"), 0, 30, 6)
        d["daily_budget_usd"] = round(_clampf(raw.get("daily_budget_usd"), 0, 100000, 0.0), 2)
        d["tts_model"] = _pick(raw.get("tts_model"), _TTS_MODELS, _TTS_ALIASES, d["tts_model"])
        lang = str(raw.get("stt_language", "")).strip().lower()
        if lang and lang.replace("-", "").isalnum() and len(lang) <= 10:
            d["stt_language"] = lang
        d["status_ack"] = _bool(raw.get("status_ack"), True)
        d["ack_thinking"] = _bool(raw.get("ack_thinking"), False)
        d["ack_delay_ms"] = _clamp(raw.get("ack_delay_ms"), 0, 15000, 2500)
        ap = raw.get("ack_phrase")
        if isinstance(ap, str) and ap.strip():
            d["ack_phrase"] = ap.strip()[:60]   # short: it's spoken aloud
    return d


def effort() -> str:
    return load()["effort"]


def model() -> str:
    return load()["model"]


def mtime() -> float:
    try:
        return _FILE.stat().st_mtime
    except OSError:
        return 0.0
