"""Per-project API usage for the on-screen panel.

Claude (Anthropic) is metered locally from the Agent SDK's per-turn `total_cost_usd`
(no admin/billing key needed) and persisted to usage.json. Today's spend is tracked
separately for the advisory daily budget. ElevenLabs character usage is pulled live;
Deepgram balance is pulled live (key needs billing:read) with a request-count fallback.
"""
from __future__ import annotations

import asyncio
import datetime
import json
import os

import httpx

from server.config import PROJECT_ROOT, settings
from server import prefs

_FILE = PROJECT_ROOT / "usage.json"
_TIMEOUT = httpx.Timeout(15.0, connect=8.0)
_TOK_KEYS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")

_data = {"claude_cost_usd": 0.0, "claude_tokens": 0, "claude_turns": 0,
         "deepgram_requests": 0, "today_date": "", "today_cost_usd": 0.0}


def _today() -> str:
    return datetime.date.today().isoformat()


def _load() -> None:
    try:
        _data.update(json.loads(_FILE.read_text()))
    except (OSError, ValueError):
        pass


def _save() -> None:
    try:
        _FILE.write_text(json.dumps(_data))
        os.chmod(_FILE, 0o600)      # spend/usage telemetry is owner-private
    except OSError:
        pass


_load()


def _tok(u) -> int:
    if not u:
        return 0
    g = u.get if isinstance(u, dict) else (lambda k, d=0: getattr(u, k, d))
    return sum(int(g(k, 0) or 0) for k in _TOK_KEYS)


def record_claude(cost, usage) -> None:
    c = float(cost or 0)
    _data["claude_cost_usd"] += c
    _data["claude_tokens"] += _tok(usage)
    _data["claude_turns"] += 1
    if _data.get("today_date") != _today():
        _data["today_date"] = _today()
        _data["today_cost_usd"] = 0.0
    _data["today_cost_usd"] += c
    _save()


def record_deepgram() -> None:
    _data["deepgram_requests"] += 1
    _save()


def today_cost() -> float:
    return _data.get("today_cost_usd", 0.0) if _data.get("today_date") == _today() else 0.0


async def _elevenlabs():
    if settings.tts_provider != "elevenlabs" or not settings.elevenlabs_api_key:
        return None
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.get("https://api.elevenlabs.io/v1/user/subscription",
                            headers={"xi-api-key": settings.elevenlabs_api_key})
        if r.status_code == 200:
            d = r.json()
            return {"used": d.get("character_count"), "limit": d.get("character_limit"),
                    "tier": d.get("tier")}
    except Exception:
        pass
    return None


_dg_pid = None


async def _deepgram():
    global _dg_pid
    if settings.stt_provider != "deepgram" or not settings.deepgram_api_key:
        return None
    out = {"requests": _data["deepgram_requests"]}
    try:
        h = {"Authorization": f"Token {settings.deepgram_api_key}"}
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            if _dg_pid is None:
                pj = (await c.get("https://api.deepgram.com/v1/projects", headers=h)).json()
                _dg_pid = pj["projects"][0]["project_id"]
            rb = await c.get(f"https://api.deepgram.com/v1/projects/{_dg_pid}/balances", headers=h)
            if rb.status_code == 200:
                bals = rb.json().get("balances", [])
                out["balance"] = round(sum(float(b.get("amount", 0)) for b in bals), 2)
                out["units"] = (bals[0].get("units") if bals else None) or "usd"
    except Exception:
        pass
    return out


async def report() -> dict:
    el, dg = await asyncio.gather(_elevenlabs(), _deepgram())
    return {
        "claude": {"cost_usd": round(_data["claude_cost_usd"], 4),
                   "tokens": _data["claude_tokens"], "turns": _data["claude_turns"],
                   "today_usd": round(today_cost(), 4),
                   "budget_usd": prefs.load()["daily_budget_usd"]},
        "elevenlabs": el,
        "deepgram": dg,
    }
